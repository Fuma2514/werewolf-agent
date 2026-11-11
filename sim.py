#!/usr/bin/env python3
"""Local mini-engine to exercise main.decide(): full 8-player werewolf per RULES,
house seats played by simple scripted bots. Measures win rates over N games."""
import random, sys, json, importlib.util

spec = importlib.util.spec_from_file_location("agent", sys.argv[1] if len(sys.argv) > 1 else "main.py")
agent = importlib.util.module_from_spec(spec); spec.loader.exec_module(agent)

N = int(sys.argv[2]) if len(sys.argv) > 2 else 200

NAMES = ["Ada","Bo","Cy","Di","Eli","Fay","Gus","Hal"]

class Game:
    def __init__(self, rng, my_role):
        seats = list(range(1,9))
        rng.shuffle(seats)
        self.me = seats[0]
        roles = ["werewolf","werewolf","seer","doctor","villager","villager","villager","villager"]
        rng.shuffle(roles)
        self.role_of = {}
        for i, s in enumerate(seats):
            self.role_of[s] = my_role if s == self.me else roles[i]
        self.wolves = sorted(s for s,r in self.role_of.items() if r=="werewolf")
        self.alive = {s: True for s in seats}
        self.revealed = {s: None for s in seats}
        self.log = []
        self.day = 0
        self.start_seat = rng.randint(1,8)
        self.last_protect = None
        self.seer_results = []
        self.wolf_votes = []
        self.outcome = None

    def view(self):
        return {"day": self.day, "phase": self.phase, "you": {
                    "seat": self.me, "name": NAMES[self.me-1], "role": self.role_of[self.me],
                    "alive": self.alive[self.me],
                    "partner": ({"seat": [w for w in self.wolves if w != self.me][0],
                                 "name": NAMES[[w for w in self.wolves if w != self.me][0]-1],
                                 "alive": self.alive[[w for w in self.wolves if w != self.me][0]]}
                                if self.role_of[self.me]=="werewolf" else None)},
                "players": [{"seat": s, "name": NAMES[s-1], "alive": self.alive[s],
                             "role": self.revealed[s]} for s in range(1,9)],
                "seerResults": self.seer_results if self.role_of[self.me]=="seer" else None,
                "lastProtect": ({"night": self.day, "seat": self.last_protect}
                                if self.role_of[self.me]=="doctor" and self.last_protect else None),
                "wolfVotes": self.wolf_votes if self.role_of[self.me]=="werewolf" else None,
                "log": self.log, "outcome": self.outcome}

    def speak(self, seat, text):
        self.log.append({"seq": len(self.log)+1, "day": self.day, "kind": "speech",
                         "seat": seat, "round": 1, "text": text})
    def vote(self, seat, target):
        self.log.append({"seq": len(self.log)+1, "day": self.day, "kind": "vote",
                         "seat": seat, "target": target})

    def wolves_win(self):
        w = sum(1 for s in self.alive if self.alive[s] and self.role_of[s]=="werewolf")
        v = sum(1 for s in self.alive if self.alive[s] and self.role_of[s]!="werewolf")
        return w >= v

    def check(self):
        if not any(self.alive[s] and self.role_of[s]=="werewolf" for s in self.alive):
            self.outcome = {"winner":"village","reason":"no_wolves","day":self.day}
        elif self.wolves_win():
            self.outcome = {"winner":"werewolves","reason":"parity","day":self.day}
        return self.outcome

    def house_decide(self, seat, kind, options=None, wolf_first_pick=None):
        """Scripted house bot: paranoid accuser, votes most-accused."""
        alive_others = [s for s in sorted(self.alive) if self.alive[s] and s != seat]
        if kind == "speak":
            # accuse the seat most accused so far, or lowest alive
            tally = {}
            for e in self.log:
                if e["kind"]=="speech" and e["seat"]!=seat and seat not in (self.wolves):
                    for m in __import__("re").finditer(r"seat\s*(\d)", e["text"]):
                        tally[int(m.group(1))] = tally.get(int(m.group(1)),0)+1
            if seat in self.wolves:
                # house wolf: accuse a non-wolf
                tgt = next((s for s in alive_others if self.role_of[s]!="werewolf"), None)
                if tally and rng.random() < 0.5:
                    best = max(tally, key=tally.get)
                    if best not in self.wolves and self.alive.get(best): tgt = best
                self.speak(seat, "I think seat %d is acting suspicious." % tgt) if tgt else self.speak(seat, "I have no strong read yet.")
                return {"text": "I think seat %d is acting suspicious." % tgt} if tgt else {"pass": True}
            if tally:
                best = max(tally, key=tally.get)
                if self.alive.get(best) and best != seat:
                    self.speak(seat, "Seat %d is the wolf, vote seat %d." % (best, best))
                    return {"text": "Seat %d is the wolf, vote seat %d." % (best, best)}
            self.speak(seat, "No wolf found yet, keep watching seat %d." % alive_others[0])
            return {"text": "No wolf found yet, keep watching seat %d." % alive_others[0]}
        if kind == "vote":
            tally = {}
            for e in self.log:
                if e["kind"]=="vote" and e["day"]==self.day and e["target"]:
                    tally[e["target"]] = tally.get(e["target"],0)+1
            if seat in self.wolves:
                pool = [s for s in alive_others if self.role_of[s]!="werewolf"]
                if not pool: return {"target": None}
                cand = {t: n for t, n in tally.items() if t in pool and t != seat}
                if cand:
                    best = max(cand, key=cand.get)
                    if best and cand[best] >= 2: return {"target": best}
                return {"target": pool[0]}
            if tally:
                best = max(tally, key=tally.get)
                if best != seat and self.alive.get(best): return {"target": best}
            return {"target": alive_others[0]} if alive_others else {"target": None}
        if kind == "kill":
            # house wolf kill: the loudest accuser of wolves, else lowest
            acc = {}
            for e in self.log:
                if e["kind"]=="speech" and e["seat"] not in self.wolves and \
                        any(w in e["text"] for w in ("wolf","Wolf")):
                    acc[e["seat"]] = acc.get(e["seat"],0)+1
            pool = [s for s in alive_others if self.role_of[s]!="werewolf"]
            if acc:
                best = max(acc, key=acc.get)
                if best in pool: return {"target": best}
            return {"target": pool[0]} if pool else {"target": None}
        if kind == "protect":
            # house doctor: protect the sole seer claimant
            claims = [e["seat"] for e in self.log if e["kind"]=="speech"
                      and "I am the seer" in e["text"] and self.alive.get(e["seat"])
                      and e["seat"] != self.last_protect]
            if claims: return {"target": claims[0]}
            pool = [s for s in alive_others if s != self.last_protect]
            return {"target": pool[0]} if pool else {"target": None}
        if kind == "inspect":
            done = {r["seat"] for r in self.seer_results}
            pool = [s for s in alive_others if s not in done]
            return {"target": pool[0]} if pool else {"target": None}

    def run(self, rng):
        # night 1 ... day 10
        while self.outcome is None and self.day < 10:
            self.day += 1
            self.phase = "night"
            self.log.append({"seq": len(self.log)+1, "day": self.day, "kind": "night"})
            # wolves vote
            lw = [w for w in self.wolves if self.alive[w]]
            lw.sort(reverse=(self.day % 2 == 0))
            picks = []
            self.wolf_votes = []
            for w in lw:
                if w == self.me:
                    t = agent.decide(self.pending("kill"), "s")["target"]
                else:
                    t = self.house_decide(w, "kill")["target"]
                if t: picks.append(t)
                self.wolf_votes.append({"night": self.day, "seat": w, "target": t})
            kill = picks[0] if len(set(picks)) == 1 and picks else None
            # doctor
            if self.role_of.get(0) is None:
                pass
            doc = [s for s in self.alive if self.alive[s] and self.role_of[s]=="doctor"]
            protect = None
            if doc:
                d = doc[0]
                pr = agent.decide(self.pending("protect"), "s") if d == self.me else self.house_decide(d, "protect")
                protect = pr["target"]
            if protect is not None: self.last_protect = protect
            # seer
            seer = [s for s in self.alive if self.alive[s] and self.role_of[s]=="seer"]
            if seer:
                s0 = seer[0]
                ins = agent.decide(self.pending("inspect"), "s") if s0 == self.me else self.house_decide(s0, "inspect")
                t = ins["target"]
                if t:
                    res = {"night": self.day, "seat": t, "werewolf": self.role_of[t]=="werewolf"}
                    if s0 == self.me: self.seer_results.append(res)
                    else: self.house_seer_knowledge = getattr(self, "house_seer_knowledge", []) + [res]
            # resolve
            if kill is not None and kill != protect:
                self.alive[kill] = False
                self.revealed[kill] = self.role_of[kill]
                self.log.append({"seq": len(self.log)+1, "day": self.day, "kind": "dawn",
                                 "died": kill, "role": self.role_of[kill]})
            else:
                self.log.append({"seq": len(self.log)+1, "day": self.day, "kind": "dawn",
                                 "died": None, "role": None})
            self.phase = "debate"
            if self.check(): break
            # debate: 2 cycles
            order = [s for s in [((self.start_seat + self.day - 2 + i) % 8) + 1 for i in range(8)] if self.alive[s]]
            for rnd in range(2):
                for s in order:
                    if not self.alive[s]: continue
                    if s == self.me:
                        self.phase = "debate"
                        p = agent.decide(self.pending("speak"), "s")
                        if "text" in p: self.speak(self.me, p["text"])
                        else: self.log.append({"seq": len(self.log)+1,"day":self.day,"kind":"pass","seat":s,"round":rnd+1})
                    else:
                        r = self.house_decide(s, "speak")
                        if r.get("text"): self.speak(s, r["text"])
            if self.check(): break
            # vote
            self.phase = "voting"
            self.log.append({"seq": len(self.log)+1,"day":self.day,"kind":"voting","order":order})
            tally = {}
            for s in order:
                if not self.alive[s]: continue
                if s == self.me:
                    r = agent.decide(self.pending("vote"), "s")
                else:
                    r = self.house_decide(s, "vote")
                t = r["target"]
                self.vote(s, t)
                if t: tally[t] = tally.get(t,0)+1
            if tally:
                top = max(tally.values())
                cands = [t for t,n in tally.items() if n == top]
                if len(cands) == 1:
                    ex = cands[0]
                    self.alive[ex] = False
                    self.revealed[ex] = self.role_of[ex]
                    self.log.append({"seq": len(self.log)+1,"day":self.day,"kind":"exile",
                                     "seat": ex, "role": self.role_of[ex],
                                     "tally": [{"seat": t, "votes": n} for t,n in tally.items()]})
            self.check()
        if self.outcome is None:
            self.outcome = {"winner": None, "reason": "max_days", "day": self.day}
        self.log.append({"seq": len(self.log)+1,"day":self.day,"kind":"end",
                         "winner": self.outcome["winner"], "reason": self.outcome["reason"],
                         "winners": []})
        self.phase = "over"
        return self.outcome

    def pending(self, kind):
        if kind == "speak":
            return {"game": 1, "decision": "speak", "max_chars": 500, "view": self.view()}
        opts = None
        if kind == "kill":
            opts = [s for s in sorted(self.alive) if self.alive[s] and self.role_of[s] != "werewolf"]
        elif kind == "protect":
            opts = [s for s in sorted(self.alive) if self.alive[s] and s != self.me and s != self.last_protect]
        elif kind == "inspect":
            opts = [s for s in sorted(self.alive) if self.alive[s] and s != self.me]
        elif kind == "vote":
            opts = [s for s in sorted(self.alive) if self.alive[s] and s != self.me]
        return {"game": 1, "decision": kind, "options": opts, "view": self.view()}

rng = random.Random(1234)
res = {"werewolf": [0,0], "seer": [0,0], "doctor": [0,0], "villager": [0,0]}
va_wolf_votes = [0,0]
days = []
for i in range(N):
    role = ["werewolf"]*15 + ["seer"]*5 + ["doctor"]*5 + ["villager"]*5
    r = role[i % 30]
    g = Game(random.Random(rng.randrange(1<<30)), r)
    out = g.run(rng)
    win = out["winner"] == ("werewolves" if r == "werewolf" else "village")
    res[r][0] += win; res[r][1] += 1
    if win and out["winner"]: days.append(out["day"])
w = res["werewolf"]; v = res["seer"]; d = res["doctor"]; vv = res["villager"]
village_w = (v[0]+d[0]+vv[0]) / max(1,(v[1]+d[1]+vv[1]))
print("wolf  : %d/%d = %.2f" % (w[0], w[1], w[0]/max(1,w[1])))
print("village: %d/%d = %.2f (seer %d/%d doctor %d/%d villager %d/%d)" %
      (v[0]+d[0]+vv[0], v[1]+d[1]+vv[1], village_w, v[0],v[1], d[0],d[1], vv[0],vv[1]))
score = 1000 * (w[0]/max(1,w[1]) + village_w) / 2
print("score ~ %.0f  avg win day %.1f" % (score, sum(days)/max(1,len(days))))
