from time import time

class Conversation():
    def __init__(self, game, move_gen, xhr, version, challenge_queue):
        self.game = game
        self.move_gen = move_gen
        self.xhr = xhr
        self.version = version
        self.challengers = challenge_queue

    command_prefix = "!"

    def react(self, line, game):
        print("*** {} [{}] {}: {}".format(self.game.url(), line.room, line.username, line.text.encode("utf-8")))
        if (line.text[0] == self.command_prefix):
            self.command(line, game, line.text[1:].lower())
        pass

    def command(self, line, game, cmd):
        if cmd == "commands" or cmd == "help":
            self.send_reply(line, "Supported commands: !name, !howto, !eval, !queue")
        elif cmd == "wait" and game.is_abortable():
            game.ping(60, 120)
            self.send_reply(line, "Waiting 60 seconds...")
        elif cmd == "name":
            self.send_reply(line, "{} (lishogi-bot v{})".format(self.move_gen, self.version))
        elif cmd == "howto":
            self.send_reply(line, "How to run your own bot: lishogi.org/api#tag/Bot")
        elif cmd == "eval" and line.room == "spectator":
            self.send_reply("I just pick at random dude.")
        elif cmd == "eval":
            self.send_reply(line, "I don't tell that to my opponent, sorry.")
        elif cmd == "queue":
            if self.challengers:
                challengers = ", ".join(["@" + challenger.challenger_name for challenger in reversed(self.challengers)])
                self.send_reply(line, "Challenge queue: {}".format(challengers))
            else:
                self.send_reply(line, "No challenges queued.")

    def send_reply(self, line, reply):
        self.xhr.chat(self.game.id, line.room, reply)


class ChatLine():
    def __init__(self, json):
        self.room = json.get("room")
        self.username = json.get("username")
        self.text = json.get("text")
