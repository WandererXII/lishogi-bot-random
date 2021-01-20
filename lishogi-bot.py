import argparse
import shogi
import model
import json
import lishogi
import logging
import multiprocessing
import traceback
import logging_pool
import signal
import sys
import time
import backoff
import threading
from config import load_config
from conversation import Conversation, ChatLine
from functools import partial
from requests.exceptions import ChunkedEncodingError, ConnectionError, HTTPError, ReadTimeout
from urllib3.exceptions import ProtocolError
from ColorLogger import enable_color_logging
from util import *
import copy
import move_gen

logger = logging.getLogger(__name__)

try:
    from http.client import RemoteDisconnected
    # New in version 3.5: Previously, BadStatusLine('') was raised.
except ImportError:
    from http.client import BadStatusLine as RemoteDisconnected

__version__ = "1.1.5"

terminated = False

def signal_handler(signal, frame):
    global terminated
    logger.debug("Recieved SIGINT. Terminating client.")
    terminated = True

signal.signal(signal.SIGINT, signal_handler)

def is_final(exception):
    return isinstance(exception, HTTPError) and exception.response.status_code < 500

def upgrade_account(li):
    if li.upgrade_to_bot_account() is None:
        return False

    logger.info("Succesfully upgraded to Bot Account!")
    return True

def watch_control_stream(control_queue, li):
    while not terminated:
        try:
            response = li.get_event_stream()
            lines = response.iter_lines()
            for line in lines:
                if line:
                    event = json.loads(line.decode('utf-8'))
                    control_queue.put_nowait(event)
                else:
                    control_queue.put_nowait({"type": "ping"})
        except:
            pass

def start(li, user_profile, config):
    challenge_config = config["challenge"]
    max_games = challenge_config.get("concurrency", 1)
    logger.info("You're now connected to {} and awaiting challenges.".format(config["url"]))
    manager = multiprocessing.Manager()
    challenge_queue = manager.list()
    control_queue = manager.Queue()
    control_stream = multiprocessing.Process(target=watch_control_stream, args=[control_queue, li])
    control_stream.start()
    busy_processes = 0
    queued_processes = 0

    with logging_pool.LoggingPool(max_games+1) as pool:
        while not terminated:
            event = control_queue.get()
            if event["type"] == "terminated":
                break
            elif event["type"] == "local_game_done":
                busy_processes -= 1
                logger.info("+++ Process Free. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))
            elif event["type"] == "challenge":
                chlng = model.Challenge(event["challenge"])
                if chlng.is_supported(challenge_config):
                    challenge_queue.append(chlng)
                    if (challenge_config.get("sort_by", "best") == "best"):
                        list_c = list(challenge_queue)
                        list_c.sort(key=lambda c: -c.score())
                        challenge_queue = list_c
                else:
                    try:
                        li.decline_challenge(chlng.id)
                        logger.info("    Decline {}".format(chlng))
                    except:
                        pass
            elif event["type"] == "gameStart":
                if queued_processes <= 0:
                    logger.debug("Something went wrong. Game is starting and we don't have a queued process")
                else:
                    queued_processes -= 1
                busy_processes += 1
                logger.info("--- Process Used. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))
                game_id = event["game"]["id"]
                pool.apply_async(play_game, [li, game_id, control_queue, user_profile, config, challenge_queue])
            while ((queued_processes + busy_processes) < max_games and challenge_queue): # keep processing the queue until empty or max_games is reached
                chlng = challenge_queue.pop(0)
                try:
                    logger.info("    Accept {}".format(chlng))
                    queued_processes += 1
                    response = li.accept_challenge(chlng.id)
                    logger.info("--- Process Queue. Total Queued: {}. Total Used: {}".format(queued_processes, busy_processes))
                except (HTTPError, ReadTimeout) as exception:
                    if isinstance(exception, HTTPError) and exception.response.status_code == 404: # ignore missing challenge
                        logger.info("    Skip missing {}".format(chlng))
                    queued_processes -= 1

    logger.info("Terminated")
    control_stream.terminate()
    control_stream.join()

@backoff.on_exception(backoff.expo, BaseException, max_time=600, giveup=is_final)
def play_game(li, game_id, control_queue, user_profile, config, challenge_queue):
    response = li.get_game_stream(game_id)
    lines = response.iter_lines()

    #Initial response of stream will be the full game info. Store it
    initial_state = json.loads(next(lines).decode('utf-8'))
    game = model.Game(initial_state, user_profile["username"], li.baseUrl, config.get("abort_time", 20))
    board = setup_board(game)
    conversation = Conversation(game, config.get("botname", "Bot without a name is a sad bot"), li, __version__, challenge_queue)

    logger.info("+++ {}".format(game))

    deferredFirstMove = False

    if len(board.move_stack) < 2:
        while not terminated:
            try:
                if not play_first_move(game, board, li):
                    deferredFirstMove = True
                break
            except (HTTPError) as exception:
                if exception.response.status_code == 400: # fallthrough
                    break
    else:
        moves = game.state["moves"].split()
        if not is_game_over(game) and is_engine_move(game, moves):
            rand_move = move_gen.random_move(board)
            li.make_move(game.id, rand_move)

    while not terminated:
        try:
            binary_chunk = next(lines)
        except(StopIteration):
            break
        try:
            upd = json.loads(binary_chunk.decode('utf-8')) if binary_chunk else None
            u_type = upd["type"] if upd else "ping"
            if u_type == "chatLine":
                conversation.react(ChatLine(upd), game)
            elif u_type == "gameState":
                game.state = upd
                moves = upd["moves"].split()
                board = update_board(board, moves[-1])
                if not is_game_over(game) and is_engine_move(game, moves):
                    if config.get("fake_think_time") and len(moves) > 9:
                        delay = min(game.clock_initial, game.my_remaining_seconds()) * 0.015
                        accel = 1 - max(0, min(100, len(moves) - 20)) / 150
                        sleep = min(0.2, delay * accel)
                        time.sleep(sleep)

                    if not deferredFirstMove:
                        rand_move = move_gen.random_move(board)
                        li.make_move(game.id, rand_move)
                    else:
                        play_first_move(game, board, li)
                        deferredFirstMove = False
                if board.turn == shogi.BLACK:
                    game.ping(config.get("abort_time", 20), (upd["wtime"] + upd["winc"]) / 1000 + 60)
                else:
                    game.ping(config.get("abort_time", 20), (upd["btime"] + upd["binc"]) / 1000 + 60)
            elif u_type == "ping":
                if game.should_abort_now():
                    logger.info("    Aborting {} by lack of activity".format(game.url()))
                    li.abort(game.id)
                    break
                elif game.should_terminate_now():
                    logger.info("    Terminating {} by lack of activity".format(game.url()))
                    if game.is_abortable():
                        li.abort(game.id)
                    break
        except (HTTPError, ReadTimeout, RemoteDisconnected, ChunkedEncodingError, ConnectionError, ProtocolError) as e:
            ongoing_games = li.get_ongoing_games()
            game_over = True
            for ongoing_game in ongoing_games:
                if ongoing_game["gameId"] == game.id:
                    game_over = False
                    break
            if not game_over:
                continue
            else:
                break

    logger.info("--- {} Game over".format(game.url()))

    # This can raise queue.NoFull, but that should only happen if we're not processing
    # events fast enough and in this case I believe the exception should be raised
    control_queue.put_nowait({"type": "local_game_done"})


def play_first_move(game, board, li):
    moves = game.state["moves"].split()
    if is_engine_move(game, moves):
        # need to hardcode first movetime since Lichess has 30 sec limit.
        rand_move = move_gen.random_move(board)
        li.make_move(game.id, rand_move)
        return True
    return False


def setup_board(game):
    if game.variant_name == "From Position":
        board = shogi.Board(makesfenfromfen(game.initial_fen))
    else:
        board = shogi.Board() # Standard
    moves = game.state["moves"].split()
    for move in moves:
        board = update_board(board, move)

    return board


def is_white_to_move(game, moves):
    return len(moves) % 2 == (0 if game.white_starts else 1)


def is_engine_move(game, moves):
    return game.is_white == is_white_to_move(game, moves)


def is_game_over(game):
    return game.state["status"] != "started"


def update_board(board, move):
    usi_move = shogi.Move.from_usi(makeusi(move))
    if board.is_legal(usi_move):
        board.push(usi_move)
    else:
        logger.debug('Ignoring illegal move {} on board {}'.format(makeusi(move), board.sfen()))
    return board

def intro():
    return r"""
    .   _/|
    .  // o\
    .  || ._)  lishogi-bot %s
    .  //__\
    .  )___(   Play on Lishogi with a bot
    """ % __version__

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description='Play on Lishogi with a bot')
    parser.add_argument('-u', action='store_true', help='Add this flag to upgrade your account to a bot account.')
    parser.add_argument('-v', action='store_true', help='Verbose output. Changes log level from INFO to DEBUG.')
    parser.add_argument('--config', help='Specify a configuration file (defaults to ./config.yml)')
    parser.add_argument('-l', '--logfile', help="Log file to append logs to.", default=None)
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.v else logging.INFO, filename=args.logfile,
                        format="%(asctime)-15s: %(message)s")
    enable_color_logging(debug_lvl=logging.DEBUG if args.v else logging.INFO)
    logger.info(intro())
    CONFIG = load_config(args.config or "./config.yml")
    li = lishogi.Lishogi(CONFIG["token"], CONFIG["url"], __version__)

    user_profile = li.get_profile()
    username = user_profile["username"]
    is_bot = user_profile.get("title") == "BOT"
    logger.info("Welcome {}!".format(username))

    if args.u is True and is_bot is False:
        is_bot = upgrade_account(li)

    if is_bot:
        start(li, user_profile, CONFIG)
    else:
        logger.error("{} is not a bot account. Please upgrade it to a bot account!".format(user_profile["username"]))
