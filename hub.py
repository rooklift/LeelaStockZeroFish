import json, queue, subprocess, sys, threading, time
import requests

# ------------------------------------------------------------------------

try:
	with open("config.json") as config_file:
		config = json.load(config_file)
		for prop in ["account", "token", "stockfish_command", "leela_command"]:
			if prop not in config:
				print(f"config.json did not have needed '{prop}' property")
				sys.exit()

except FileNotFoundError:
	print("Couldn't load config.json")
	sys.exit()

except json.decoder.JSONDecodeError:
	print("config.json seems to be illegal JSON")
	sys.exit()

# ------------------------------------------------------------------------

headers = {"Authorization": f"Bearer {config['token']}"}

main_log = queue.Queue()

active_game = None
active_game_MUTEX = threading.Lock()

stockfish = None
leela = None

# ------------------------------------------------------------------------

class Engine():

	def __init__(self, command, shortname):

		self.shortname = shortname

		self.process = subprocess.Popen(command, shell = False,
										stdin = subprocess.PIPE,
										stdout = subprocess.PIPE,
										stderr = subprocess.PIPE,
										)

		# Make a thread that puts this engine's stdout onto a queue which we read when needed...
		# I forget why I bothered with this now. Seems redundant. I was probably worried about
		# the pipe filling up.

		self.stdout_queue = queue.Queue()
		threading.Thread(target = stdout_to_queue, args = (self.process, self.stdout_queue, self.shortname), daemon = True).start()

		# Make a thread that puts this engine's stderr into a log...
		# I think it's necessary to do SOMETHING with stderr (so it doesn't build up and hang the engine).
		# As an alternative, we could send it to devnull.

		threading.Thread(target = stderr_to_log, args = (self.process, f"{self.shortname}_stderr.txt"), daemon = True).start()

	def send(self, msg):

		msg = msg.strip()
		b = bytes(msg + "\n", encoding = "ascii")
		self.process.stdin.write(b)
		self.process.stdin.flush()
		log(self.shortname + " <- " + msg)

	def get_best_move(self):

		# Assumes the go command has already been sent

		while 1:
			z = self.stdout_queue.get()
			# log(self.shortname + " :: " + z)

			if "bestmove" in z:
				tokens = z.split()
				return tokens[1]

	def validate(self, test_move):

		# Assumes the go command has already been sent, and MultiPV is on
		# Returns the move we should actually play

		test_score = None
		best_move = None
		best_score = None

		while 1:
			z = self.stdout_queue.get()
			# log(self.shortname + " :: " + z)

			if f"pv {test_move}" in z:		# Sketchy because UCI allows random whitespace

				tokens = z.split()

				try:
					score_index = tokens.index("cp") + 1
					test_score = int(tokens[score_index])
				except ValueError:
					try:
						mate_index = tokens.index("mate") + 1

						mate_in = int(tokens[mate_index])

						if mate_in > 0:
							test_score = 100000 - (mate_in * 1000)
						else:
							test_score = -100000 + (-mate_in * 1000)

					except ValueError:
						pass

			if "multipv 1 " in z:		# Space is needed

				tokens = z.split()

				move_index = tokens.index("pv") + 1
				best_move = tokens[move_index]

				try:
					score_index = tokens.index("cp") + 1
					best_score = int(tokens[score_index])
				except ValueError:
					try:
						mate_index = tokens.index("mate") + 1

						mate_in = int(tokens[mate_index])

						if mate_in > 0:
							best_score = 100000 - (mate_in * 1000)
						else:
							best_score = -100000 + (-mate_in * 1000)

					except ValueError:
						pass

			if "bestmove" in z:
				break

		log(f"{test_move} ({test_score}) vs {best_move} ({best_score})")

		if test_score != None and best_score != None:
			diff = best_score - test_score					# Higher diff is worse
			if diff > 150:
				log("FAILED")
				return best_move
			else:
				return test_move

		# We didn't get both scores, likely because test_move wasn't seen in the top lines.

		if best_move != None:
			log("FAILED")
			return best_move
		else:
			return test_move

# ------------------------------------------------------------------------

def main():

	global stockfish
	global leela

	threading.Thread(target = logger_thread, args = ("log.txt", main_log), daemon = True).start()
	log(f"-- STARTUP -- at {time.strftime('%a, %d %b %Y %H:%M:%S', time.localtime())} " + "-" * 40)

	stockfish = Engine(config["stockfish_command"], "SF")
	leela = Engine(config["leela_command"], "LZ")

	stockfish.send("uci")
	leela.send("uci")

	stockfish.send("setoption name MultiPV value 10")

	event_stream = requests.get("https://lichess.org/api/stream/event", headers = headers, stream = True)

	for line in event_stream.iter_lines():

		if line:

			dec = line.decode('utf-8')
			j = json.loads(dec)

			if j["type"] == "challenge":
				handle_challenge(j["challenge"])

			if j["type"] == "gameStart":
				start_game(j["game"]["id"])

	log("ERROR: Main event stream closed.")


def handle_challenge(challenge):

	global active_game
	global active_game_MUTEX

#	"challenge": {
#		"id": "7pGLxJ4F",
#		"status": "created",
#		"rated": true,
#		"color": "random",
#		"variant": {"key": "standard", "name": "Standard", "short": "Std"},
#		"timeControl": {"type": "clock", "limit":300, "increment":25, "show": "5+25"},
#		"challenger": {"id": "lovlas", "name": "Lovlas", "title": "IM", "rating": 2506, "patron": true, "online": true, "lag": 24},
#		"destUser": {"id": "thibot", "name": "thibot", "title": null, "rating": 1500, "provisional": true, "online": true, "lag": 45},
#		"perf": {"icon": "#", "name": "Rapid"}
#	}

	log(f"Incoming challenge from {challenge['challenger']['name']} -- {challenge['timeControl']['show']} (rated: {challenge['rated']})")

	accepting = True

	# Already playing...

	with active_game_MUTEX:
		if active_game:
			accepting = False

	# Variants...

	if challenge["variant"]["key"] != "standard":
		accepting = False

	# Time control...

	if challenge["timeControl"]["type"] != "clock":
		accepting = False
	elif challenge["timeControl"]["limit"] < 60 or challenge["timeControl"]["limit"] > 300:
		accepting = False
	elif challenge["timeControl"]["increment"] < 1 or challenge["timeControl"]["increment"] > 10:
		accepting = False

	if not accepting:
		decline(challenge["id"])
	else:
		accept(challenge["id"])


def decline(challengeId):

	log(f"Declining challenge {challengeId}.")
	r = requests.post(f"https://lichess.org/api/challenge/{challengeId}/decline", headers = headers)
	if r.status_code != 200:
		try:
			log(r.json())
		except:
			log(f"decline returned {r.status_code}")

def accept(challengeId):

	log(f"Accepting challenge {challengeId}.")
	r = requests.post(f"https://lichess.org/api/challenge/{challengeId}/accept", headers = headers)
	if r.status_code != 200:
		try:
			log(r.json())
		except:
			log(f"accept returned {r.status_code}")

def start_game(gameId):

	global active_game
	global active_game_MUTEX

	game = Game(gameId)
	autoabort = False

	with active_game_MUTEX:
		if active_game:
			autoabort = True
		else:
			active_game = game

	if autoabort:
		log("WARNING: game started but I seem to be in a game")
		game.abort()
		return

	threading.Thread(target = runner, args = (game, )).start()
	log(f"Game {gameId} started")


def runner(game):

	global stockfish
	global leela

	stockfish.send("ucinewgame")
	leela.send("ucinewgame")

	game.loop()


def sign(num):
	if num < 0:
		return -1
	if num > 0:
		return 1
	return 0


def log(msg):
	main_log.put(msg)

# ------------------------------------------------------------------------

class Game():

	def __init__(self, gameId):

		self.gameId = gameId
		self.gameFull = None
		self.colour = None
		self.events = requests.get(f"https://lichess.org/api/bot/game/stream/{gameId}", headers = headers, stream = True)

		self.moves_made = 0
		self.vetoes = 0

	def loop(self):

		for line in self.events.iter_lines():

			if not line:		# Filter out keep-alive newlines
				continue

			# Each line is a JSON object containing a type field. Possible values are:
			#		gameFull	-- Full game data. All values are immutable, except for the state field.
			#		gameState	-- Current state of the game. Immutable values not included.
			#		chatLine 	-- Chat message sent by a user (or the bot itself) in the room "player" or "spectator".

			dec = line.decode('utf-8')
			j = json.loads(dec)

			if j["type"] == "gameFull":

				self.gameFull = j

				log(j)

				if j["white"]["name"].lower() == config["account"].lower():
					self.colour = "white"
				elif j["black"]["name"].lower() == config["account"].lower():
					self.colour = "black"

				self.handle_state(j["state"])

			elif j["type"] == "gameState":

				self.handle_state(j)

		log("Game stream closed.")
		self.finish()


	def handle_state(self, state):

		if self.colour == None:
			log("ERROR: handle_state() called but my colour is unknown")
			self.abort()

		moves = []

		if state["moves"]:
			moves = state["moves"].split()

		if len(moves) % 2 == 0 and self.colour == "black":
			return
		if len(moves) % 2 == 1 and self.colour == "white":
			return

		if len(moves) > 0:
			log("-----------------")
			log(f"Opponent played {moves[-1]}")

		self.play(state)


	def play(self, state):

		global stockfish
		global leela

		log("-----------------")

		wtime_minus_1s = max(1, state["wtime"] - 1100)
		btime_minus_1s = max(1, state["btime"] - 1100)

		leela.send(f"position {self.gameFull['initialFen']} moves {state['moves']}")
		leela.send(f"go wtime {wtime_minus_1s} btime {btime_minus_1s} winc {state['winc']} binc {state['binc']}")

		provisional_move = leela.get_best_move()

		stockfish.send(f"position {self.gameFull['initialFen']} moves {state['moves']}")
		stockfish.send(f"go movetime 1000")

		actual_move = stockfish.validate(provisional_move)

		self.moves_made += 1
		if actual_move != provisional_move:
			self.vetoes += 1

		self.move(actual_move)
		log(f"Leela wants {provisional_move} ; playing {actual_move}")


	def resign(self):

		log(f"Resigning game {self.gameId}.")

		requests.post(f"https://lichess.org/api/bot/game/{self.gameId}/resign", headers = headers)
		if r.status_code != 200:
			try:
				log(r.json())
			except:
				log(f"resign returned {r.status_code}")

		self.finish()


	def abort(self):

		log(f"Aborting game {self.gameId}.")

		r = requests.post(f"https://lichess.org/api/bot/game/{self.gameId}/abort", headers = headers)
		if r.status_code != 200:
			try:
				log(r.json())
			except:
				log(f"abort returned {r.status_code}")

		self.finish()


	def move(self, move):		# move in UCI format

		r = requests.post(f"https://lichess.org/api/bot/game/{self.gameId}/move/{move}", headers = headers)
		if r.status_code != 200:
			log(f"ERROR: move failed in game {self.gameId}")
			try:
				log(r.json())
			except:
				log(f"move returned {r.status_code}")


	def finish(self):

		global active_game
		global active_game_MUTEX

		log(f"Moves: {self.moves} ; Vetoes: {self.vetoes}")

		with active_game_MUTEX:
			if active_game == self:
				active_game = None
				log("active_game set to None")
				log("-----------------------------------------------------------------")
			else:
				log("active_game not touched")

# ------------------------------------------------------------------------

def stdout_to_queue(process, q, shortname):

	while 1:
		z = process.stdout.readline().decode("utf-8")

		if z == "":
			log(f"WARNING: got EOF while reading from {shortname}.")
			return
		elif z.strip() == "":
			pass
		else:
			q.put(z.strip())


def stderr_to_log(process, filename):

	logfile = open(filename, "a")

	while 1:
		z = process.stderr.readline().decode("utf-8")

		if z == "":
			logfile.write("EOF" + "\n")
			return
		else:
			logfile.write(z)


def logger_thread(filename, q):

	logfile = open(filename, "a")

	flush_time = time.monotonic()

	while 1:

		try:

			msg = q.get(block = False)

			msg = str(msg).strip()
			logfile.write(msg + "\n")
			print(msg)

		except queue.Empty:

			if time.monotonic() - flush_time > 1:
				logfile.flush()
				flush_time = time.monotonic()

			time.sleep(0.1)		# Essential since we're not blocking on the read.

# ------------------------------------------------------------------------

if __name__ == "__main__":
	main()
