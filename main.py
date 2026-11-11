#!/usr/bin/env python3
"""werewolf-agent: heuristic Werewolf agent (stdlib only, deterministic).

Runs as `python3 -P -s /bot/main.py`. Polls POST /observe on MAKEFUN_ENV_URL and
answers every pending decision (speak/vote/kill/protect/inspect) with a pure
function of that decision's view (plus MAKEFUN_TRIAL_SEED for tie jitter). No
wall clock, no unseeded randomness, one env request at a time.
"""

import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))
WOLF = "werewolf"


# ------------------------------------------------------------------ http


def post(url, body, timeout=90):
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"content-type": "application/json", "accept": "application/json"})
    for attempt in range(4):
        try:
            with _OPENER.open(req, timeout=timeout) as res:
                raw = res.read().decode("utf-8", "replace")
                try:
                    return res.status, json.loads(raw)
                except ValueError:
                    return res.status, None
        except urllib.error.HTTPError as err:
            raw = ""
            try:
                raw = err.read().decode("utf-8", "replace")
            except Exception:
                pass
            if err.code in (400, 409):  # refused, changes nothing: report
                try:
                    return err.code, json.loads(raw)
                except ValueError:
                    return err.code, None
            time.sleep(1.5 + attempt)
        except Exception:
            time.sleep(1.5 + attempt)
    return 0, None


# ------------------------------------------------------------------ game state


def _norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())


class GameState:
    """Everything the view carries, parsed into a strategy-friendly shape."""

    def __init__(self, view):
        self.view = view
        me = view["you"]
        self.me = me["seat"]
        self.role = me["role"]
        self.alive = me["alive"]
        self.day = view["day"]
        self.phase = view["phase"]
        self.players = {}   # seat -> {"name","alive","role"}
        for p in view["players"]:
            self.players[p["seat"]] = {
                "name": p["name"], "alive": p["alive"], "role": p["role"]}
        self.name2seat = {_norm(p["name"]): s for s, p in self.players.items()}
        self.partner = (me.get("partner") or {}).get("seat") if me.get("partner") else None
        self.wolf_seats = set()
        if self.role == WOLF:
            self.wolf_seats = {self.me}
            if self.partner:
                self.wolf_seats.add(self.partner)
        self.speeches = []      # (day, seat, text)
        self.votes = []         # (day, voter, target)
        self.exiled = []        # (seat, role)
        self.night_deaths = []  # (seat, role)
        self.last_dawn_day = 0
        self.claims = []        # {"seat","kind","day"}
        self.findings = []      # {"seat"claimer,"target","wolf"bool,"day"}
        self.accusations = []   # {"seat"accuser,"target","day"}
        self.defenses = []      # {"seat","target","day"}
        for e in view.get("log") or []:
            k = e.get("kind")
            d = e.get("day", 0)
            if k == "dawn":
                self.last_dawn_day = d
                if e.get("died"):
                    self.night_deaths.append((e["died"], e.get("role")))
            elif k == "speech":
                self.speeches.append((d, e["seat"], e.get("text") or ""))
                self._parse_speech(d, e["seat"], e.get("text") or "")
            elif k == "vote":
                self.votes.append((d, e["seat"], e.get("target")))
            elif k == "exile":
                self.exiled.append((e["seat"], e.get("role")))
            elif k == "end":
                pass
        self.alive_seats = [s for s, p in sorted(self.players.items()) if p["alive"]]
        self.revealed_wolves = {s for s, r in self.exiled if r == WOLF} | \
            {s for s, r in self.night_deaths if r == WOLF}
        self.revealed_village = ({s for s, r in self.exiled if r and r != WOLF} |
                                 {s for s, r in self.night_deaths if r and r != WOLF})
        self.known_wolves = set(self.revealed_wolves)
        self.known_village = set(self.revealed_village)
        if self.role == WOLF:
            self.known_wolves |= self.wolf_seats
        if self.role == "seer":
            for r in view.get("seerResults") or []:
                (self.known_wolves if r["werewolf"] else self.known_village).add(r["seat"])
        if self.role == "doctor" and view.get("lastProtect"):
            self.last_protect_seat = view["lastProtect"]["seat"]
        else:
            self.last_protect_seat = None
        # suspicion and threat
        self.sus = self._suspicion()
        self.threat = self._threat()

    # ---- text parsing

    def _parse_speech(self, day, seat, text):
        t = text
        low = t.lower()
        # seer / doctor claims
        if re.search(r"\bi am the seer\b|\bi'?m the seer\b|\bas the seer\b|"
                     r"\bmy inspection|\bi inspected|\bi have inspected|"
                     r"\bseer check", low):
            self.claims.append({"seat": seat, "kind": "seer", "day": day})
        if re.search(r"\bi am the doctor\b|\bi'?m the doctor\b|\bas the doctor\b", low):
            self.claims.append({"seat": seat, "kind": "doctor", "day": day})
        # claimed inspection results: "<target> ... (is|is not) ... wolf-ish"
        for m in re.finditer(
                r"(?:inspected|checked|looked at|my inspection of)\s*"
                r"(?:seat\s*)?(\d)", low):
            target = int(m.group(1))
            tail = low[m.end(): m.end() + 60]
            wolf = bool(re.search(r"\b(is\s+a\s+)?(werewolf|wolf)\b", tail)) and \
                not re.search(r"not\s+a\s+(werewolf|wolf)", tail)
            self.findings.append({"seat": seat, "target": target, "wolf": wolf, "day": day})
        for m in re.finditer(r"(?:inspected|checked)\s+(?:seat\s*)?(\d+)\s*[,.]?\s*"
                             r"(?:and\s+)?they\s+are\s+not|clean", low):
            pass
        # clean results ("seat N is not a werewolf / is clean / innocent")
        for m in re.finditer(r"(?:seat\s*)?(\d)\s+(?:is|are)\s+not\s+a\s+"
                             r"(?:werewolf|wolf)", low):
            self.findings.append({"seat": seat, "target": int(m.group(1)),
                                  "wolf": False, "day": day})
        # accusations: "<X> is/are/must be the werewolf|a wolf|suspicious|lying"
        targets = self._refs(t)
        neg = bool(re.search(r"werewolf|wolf|suspicious|suspect|lying|liar|vote|"
                             r"exile| impostor|imposter|fake|scum", low))
        pos = bool(re.search(r"\btrust\b|\bclean\b|\binnocent\b|\bnot the wolf\b|"
                             r"\bdefend\b|\bbelieve\b", low))
        for tgt in targets:
            if tgt == seat:
                continue
            if neg:
                self.accusations.append({"seat": seat, "target": tgt, "day": day})
            if pos:
                self.defenses.append({"seat": seat, "target": tgt, "day": day})

    def _refs(self, text):
        """Seats referenced in a statement (seat numbers and player names)."""
        out = []
        for m in re.finditer(r"seat\s*(\d)", text, re.I):
            s = int(m.group(1))
            if s in self.players:
                out.append(s)
        words = set(re.findall(r"[A-Za-z]+", text))
        for w in words:
            s = self.name2seat.get(_norm(w))
            if s is not None and s not in out:
                out.append(s)
        return out

    # ---- scoring

    def _claimants(self, kind):
        return sorted({c["seat"] for c in self.claims if c["kind"] == kind})

    def _seer_cred(self):
        """credibility score for each seer claimant."""
        cred = {}
        claimants = self._claimants("seer")
        for c in claimants:
            sc = 1.0
            if len(claimants) > 1:
                sc -= 2.0  # counter-claims: at most one is real
            for f in self.findings:
                if f["seat"] != c:
                    continue
                role = self.players[f["target"]]["role"] if f["target"] in self.players else None
                if role is None:
                    continue
                if f["wolf"] and role == WOLF:
                    sc += 4.0
                elif f["wolf"] and role != WOLF:
                    sc -= 5.0
                elif not f["wolf"] and role == WOLF:
                    sc -= 5.0
                else:
                    sc += 1.5
            if c in self.known_wolves:
                sc = -10.0
            cred[c] = sc
        return cred

    def _suspicion(self):
        """Posterior-ish wolf-suspicion per seat, from the public record."""
        sus = {}
        for s in self.players:
            if s in self.known_wolves:
                sus[s] = 100.0
            elif s in self.known_village:
                sus[s] = -100.0
            else:
                sus[s] = 1.0
        cred = self._seer_cred()
        # dead players were victims of the wolves (wolves never kill wolves)
        victims = {s for s, _ in self.night_deaths}
        for v in victims:
            if v in self.known_wolves or v in self.known_village:
                continue
            # the victim was village: whoever they accused is interesting to wolves
            for a in self.accusations:
                if a["seat"] == v and sus.get(a["target"], 0) > -50:
                    sus[a["target"]] += 1.2
        # claims
        seer_claimants = self._claimants("seer")
        for c in seer_claimants:
            if c in self.known_wolves or c in self.known_village:
                continue
            if len(seer_claimants) == 1:
                sus[c] -= 2.0
            else:
                sus[c] += 3.0  # counter-claim fight: one is a wolf
            if cred.get(c, 0) >= 4:
                sus[c] -= 3.0
            elif cred.get(c, 0) <= -3:
                sus[c] += 6.0
        doctor_claimants = self._claimants("doctor")
        for c in doctor_claimants:
            if c not in self.known_wolves and c not in self.known_village:
                sus[c] -= 0.8 if len(doctor_claimants) == 1 else 2.0
        # claimed findings by trusted seers shift the target
        for f in self.findings:
            if f["seat"] in self.known_wolves:
                # a wolf's "finding" frames its target: mild trust discount for target
                if sus.get(f["target"], 0) > -50 and f["wolf"]:
                    sus[f["target"]] -= 0.4
                continue
            w = 2.5 if cred.get(f["seat"], 0) >= 3 else (0.8 if f["seat"] in seer_claimants else 0.0)
            if w and sus.get(f["target"], -100) > -50 and sus.get(f["target"], 0) < 50:
                sus[f["target"]] += w if f["wolf"] else -w
        # accusations weighted by accuser's own standing
        acc_sus = dict(sus)
        for a in self.accusations:
            acc = a["seat"]
            tgt = a["target"]
            if acc == tgt:
                continue
            if acc in self.known_wolves:
                if sus.get(tgt, 0) > -50:
                    sus[tgt] -= 0.5  # wolves accuse village
            elif acc in self.known_village:
                if sus.get(tgt, 0) > -50:
                    sus[tgt] += 1.0
            else:
                w = 0.7 / (1.0 + max(0.0, acc_sus.get(acc, 1.0)))
                if sus.get(tgt, 0) > -50:
                    sus[tgt] += w
        # defenses
        for d in self.defenses:
            if d["seat"] in self.known_wolves:
                if sus.get(d["target"], 0) < 50:
                    sus[d["target"]] += 0.5  # a wolf defends its own
        # votes: rewarded/punished by what the target turned out to be
        outcome = {}
        for s, r in self.exiled:
            outcome[s] = r
        for vday, voter, tgt in self.votes:
            if tgt is None or tgt not in outcome:
                continue
            r = outcome[tgt]
            if voter in self.known_wolves:
                continue
            if r == WOLF and sus.get(voter, 0) > -50:
                sus[voter] -= 1.2
            elif r != WOLF and sus.get(voter, 0) > -50:
                sus[voter] += 1.0
        # voting-when-accused-of-nothing baseline decay toward 1.0 for quiet players
        return sus

    def _threat(self):
        """How dangerous each seat is to the werewolves (wolf-only view)."""
        th = {s: 0.0 for s in self.players}
        cred = self._seer_cred()
        for c, sc in cred.items():
            th[c] += 3.0 + max(0.0, sc)
        for f in self.findings:
            if f["wolf"] and f["target"] in self.wolf_seats:
                th[f["seat"]] += 3.0
        for a in self.accusations:
            if a["target"] in self.wolf_seats:
                th[a["seat"]] += 1.0
        for vday, voter, tgt in self.votes:
            if tgt in self.wolf_seats and voter not in self.wolf_seats:
                th[voter] += 0.8
        return th


# ------------------------------------------------------------------ helpers


def jitter(seed, *parts):
    h = hashlib.sha256(("wf|" + str(seed) + "|" + "|".join(map(str, parts))).encode()).digest()
    return int.from_bytes(h[:4], "big")


def top_by(scores, candidates):
    """argmax by score, ties broken by lower seat."""
    best = None
    for s in sorted(candidates):
        v = scores.get(s, 0.0)
        if best is None or v > best[1]:
            best = (s, v)
    return best[0] if best else None


def current_votes(g):
    """Votes cast so far today, in order."""
    return [(v, t) for vd, v, t in g.votes if vd == g.day and t is not None]


def plurality(cur):
    tally = {}
    for _, t in cur:
        tally[t] = tally.get(t, 0) + 1
    if not tally:
        return None, 0
    t, n = max(tally.items(), key=lambda kv: (kv[1], -kv[0]))
    return t, n


# ------------------------------------------------------------------ decisions


def seer_inspect(g, options, seed):
    # first: bust a counter-claimant or validate a rival claim
    claimants = [c for c in g._claimants("seer") if c in options and c != g.me]
    inspected = {r["seat"] for r in (g.view.get("seerResults") or [])}
    for c in claimants:
        if c not in inspected:
            return c
    unknown = [s for s in options if s not in inspected]
    if not unknown:
        return options[0] if options else None
    # prefer the seat the table already suspects: a hit there converts to votes
    acc = {}
    for a in g.accusations:
        if a["target"] in unknown and a["seat"] not in g.known_wolves and a["seat"] != g.me:
            acc[a["target"]] = acc.get(a["target"], 0) + 1
    scores = {s: g.sus.get(s, 1.0) + 2.0 * acc.get(s, 0)
              + jitter(seed, "si", s, g.day) * 1e-4 for s in unknown}
    return top_by(scores, unknown)


def wolf_kill(g, options, seed):
    # second wolf always matches the partner's pick: guarantees the kill
    for v in (g.view.get("wolfVotes") or []):
        if v.get("night") == g.day and v.get("seat") in g.wolf_seats and \
                v.get("seat") != g.me and v.get("target") in options:
            return v["target"]
    # otherwise kill the biggest village threat
    t = top_by(g.threat, options)
    if t is not None and g.threat.get(t, 0) >= 1.0:
        return t
    # fall back: whoever the village trusts most (low suspicion, village-aligned)
    trusted = [s for s in options if g.sus.get(s, 1.0) < 2.0]
    if trusted:
        return top_by({s: -g.sus.get(s, 0.0) * 10 + jitter(seed, "wk", s, g.day) * 1e-6
                       for s in trusted}, trusted)
    return top_by({s: jitter(seed, "wk2", s, g.day) for s in options}, options)


def doctor_protect(g, options, seed):
    # protect the most credible living seer claimant first
    cred = g._seer_cred()
    claimants = [c for c in g._claimants("seer")
                 if c in options and c != g.last_protect_seat
                 and cred.get(c, 0) > 0 and g.players[c]["alive"]]
    if claimants:
        return top_by({c: cred[c] for c in claimants}, claimants)
    # then a verified wolf-accuser
    acc = {}
    for a in g.accusations:
        if a["target"] in g.known_wolves and g.players[a["seat"]]["alive"] and \
                a["seat"] in options and a["seat"] != g.last_protect_seat:
            acc[a["seat"]] = acc.get(a["seat"], 0) + 1
    if acc:
        return top_by(acc, acc)
    # then the least suspicious alive seat (a likely future seer) but never last night's
    pool = [s for s in options if s != g.last_protect_seat]
    if not pool:
        pool = options
    return top_by({s: -g.sus.get(s, 0.0) + jitter(seed, "dp", s, g.day) * 1e-6
                   for s in pool}, pool)


def village_vote(g, options, seed):
    cur = current_votes(g)
    lead, n = plurality(cur)
    # follow a trusted seer's wolf finding
    cred = g._seer_cred()
    for f in reversed(g.findings):
        if f["wolf"] and cred.get(f["seat"], 0) >= 3 and f["target"] in options \
                and g.players[f["target"]]["alive"]:
            return f["target"]
    pick = top_by(g.sus, options)
    # never vote a credible living seer claimant
    cred = g._seer_cred()
    if pick is not None and cred.get(pick, 0) >= 3 and g.players[pick]["alive"] and len(options) > 1:
        pick = top_by({s: g.sus.get(s, 0.0) for s in options if s != pick}, options)
    # bandwagon: join a leading vote if it is among our top suspects
    if lead is not None and lead in options and n >= 2:
        ranked = sorted(options, key=lambda s: -g.sus.get(s, 0.0))
        if lead in ranked[:2]:
            return lead
    if pick is not None and g.sus.get(pick, 0) >= 1.0:
        return pick
    if lead is not None and lead in options:
        return lead
    return pick


def wolf_vote(g, options, seed):
    cur = current_votes(g)
    lead, n = plurality(cur)
    pool = [s for s in options if s not in g.wolf_seats]
    if not pool:
        return lead if lead in options else None
    # blend in: join a bandwagon that is not targeting us or our partner
    if lead is not None and lead in pool and n >= 2:
        return lead
    # scapegoat: a mid-suspicion village seat (credible frame), never the top suspect
    # the village already distrusts (too easy), and not whoever defends us
    scores = {}
    for s in pool:
        base = g.sus.get(s, 1.0)
        scores[s] = 1.0 - abs(base - 1.5) + jitter(seed, "wv", s, g.day) * 1e-4
    pick = top_by(scores, pool)
    if pick is None:
        pick = pool[0]
    return pick


# ------------------------------------------------------------------ statements


def refs_str(g, seat):
    return "seat %d" % seat


def seer_statement(g, seed):
    results = sorted((g.view.get("seerResults") or []), key=lambda r: r["night"])
    cred = g._seer_cred()
    rival = [c for c in g._claimants("seer") if c != g.me]
    wolf_hits = [r for r in results if r["werewolf"]]
    parts = []
    if wolf_hits:
        r = wolf_hits[-1]
        parts.append("I am the seer. Night %d I inspected %s: WEREWOLF."
                     % (r["night"], refs_str(g, r["seat"])))
        if len(wolf_hits) > 1:
            parts.append("My other findings: " + "; ".join(
                "%s %s" % (refs_str(g, x["seat"]), "WOLF" if x["werewolf"] else "clean")
                for x in wolf_hits[:-1]))
        cleans = [r for r in results if not r["werewolf"]]
        if cleans:
            parts.append("Also clean: " + ", ".join(refs_str(g, r["seat"]) for r in cleans))
        if wolf_hits[-1]["seat"] in options_alive(g):
            parts.append("Vote seat %d today." % wolf_hits[-1]["seat"])
    else:
        found = ["%s clean" % refs_str(g, r["seat"]) for r in results]
        if rival:
            parts.append("I am the real seer; seat %s is lying, I will inspect them tonight."
                         % ", seat ".join(str(c) for c in rival))
        elif g.day >= 2 or len(results) >= 2:
            parts.append("I am the seer. My inspections: " + (", ".join(found) if found else "none yet") + ".")
        else:
            return None  # stay hidden night 1 with no wolf found
    if len(parts) == 0:
        return None
    return " ".join(parts)[:500]


def options_alive(g):
    return set(g.alive_seats)


def wolf_statement(g, seed):
    # 1) counter a real seer claim that threatens us
    cred = g._seer_cred()
    rival = None
    for c, sc in sorted(cred.items()):
        if sc >= 3 and c not in g.wolf_seats and g.players[c]["alive"]:
            rival = c
            break
    if rival is not None:
        return ("I am the seer, not seat %d. Night %d I inspected seat %d: WEREWOLF. "
                "They are lying to frame me. Vote seat %d."
                % (rival, max(1, g.day - 1), rival, rival))[:500]
    # 2) frame a mid-suspicion village seat
    pool = [s for s in g.alive_seats if s not in g.wolf_seats]
    if g.partner and g.players.get(g.partner, {}).get("alive"):
        accused_partner = any(a["target"] == g.partner for a in g.accusations)
        if accused_partner:
            accusers = {a["seat"] for a in g.accusations if a["target"] == g.partner
                        and a["seat"] not in g.wolf_seats and g.players[a["seat"]]["alive"]}
            if accusers:
                t = sorted(accusers)[0]
                return ("Seat %d keeps accusing innocent people without proof. Watch them; "
                        "I think seat %d is our wolf." % (t, t))[:500]
    cur = current_votes(g)
    lead, n = plurality(cur)
    if lead is not None and lead in pool and n >= 2:
        return ("I agree, seat %d has been acting strange. I will vote seat %d too."
                % (lead, lead))[:500]
    scores = {s: g.sus.get(s, 1.0) + jitter(seed, "ws", s, g.day) * 1e-4 for s in pool}
    t = top_by(scores, pool)
    if t is None:
        return None
    if g.day <= 1:
        return "From the votes and talk so far I trust seat %d the least. Seat %d looks like the wolf to me." % (t, t)[:500]
    return "Seat %d has voted oddly and never adds real information. I am voting seat %d." % (t, t)[:500]


def village_statement(g, seed):
    cred = g._seer_cred()
    # endorse a credible seer claim
    for f in reversed(g.findings):
        if f["wolf"] and cred.get(f["seat"], 0) >= 3 and g.players[f["target"]]["alive"]:
            return ("Seat %d checked seat %d and found a WEREWOLF; that checks out. "
                    "Everyone vote seat %d." % (f["seat"], f["target"], f["target"]))[:500]
    claimants = [c for c in g._claimants("seer") if g.players[c]["alive"]]
    if len(claimants) == 1 and cred.get(claimants[0], 0) >= 1:
        c = claimants[0]
        return ("Seat %d is the only seer claim and their results have held up. "
                "Trust them; I will follow their inspections." % c)[:500]
    if len(claimants) > 1:
        return ("We have two seer claims, so one is a lying wolf. Seat %s, defend "
                "yourselves with details; I will vote the less convincing one."
                % ", seat ".join(map(str, claimants)))[:500]
    cur = current_votes(g)
    lead, n = plurality(cur)
    pick = top_by(g.sus, [s for s in g.alive_seats if s != g.me])
    if lead is not None and pick is not None and lead != pick and n >= 2 \
            and g.sus.get(lead, 0) >= g.sus.get(pick, 0) - 1.0:
        return "I also suspect seat %d. Voting seat %d with the group." % (lead, lead)[:500]
    if pick is None:
        return None
    why = "has accused without evidence" if any(a["target"] == pick for a in g.accusations) \
        else "has been too quiet and their votes look odd"
    return "Seat %d %s. I say seat %d is the wolf; vote seat %d." % (pick, why, pick, pick)[:500]


def speak(g, max_chars, seed):
    if g.role == "seer":
        text = seer_statement(g, seed)
    elif g.role == WOLF:
        text = wolf_statement(g, seed)
    else:
        text = village_statement(g, seed)
    if not text:
        return {"pass": True}
    text = text.strip()[:max_chars].strip()
    if not text:
        return {"pass": True}
    return {"text": text}


# ------------------------------------------------------------------ dispatch


def decide(p, seed):
    g = GameState(p["view"])
    d = p["decision"]
    out = {"game": p["game"], "decision": d}
    if d == "speak":
        out.update(speak(g, p.get("max_chars", 500), seed))
        return out
    options = [o for o in (p.get("options") or []) if o != g.me] if d == "protect" \
        else list(p.get("options") or [])
    if d == "kill":
        t = wolf_kill(g, options, seed) if g.role == WOLF else None
    elif d == "protect":
        t = doctor_protect(g, options, seed)
    elif d == "inspect":
        t = seer_inspect(g, options, seed) if g.role == "seer" else None
    elif d == "vote":
        if g.role == WOLF:
            t = wolf_vote(g, options, seed)
        else:
            t = village_vote(g, options, seed)
    else:
        t = None
    if d == "vote" and t is None:
        out["target"] = None
    elif t is None:
        out["target"] = None
        if d == "speak":
            out["pass"] = True
    else:
        out["target"] = t
    return out


# ------------------------------------------------------------------ main loop


def main():
    env_url = os.environ.get("MAKEFUN_ENV_URL", "").rstrip("/")
    if not env_url:
        print("MAKEFUN_ENV_URL missing", file=sys.stderr)
        return 2
    seed = os.environ.get("MAKEFUN_TRIAL_SEED", "0")
    attempts = {}
    while True:
        st, obs = post(env_url + "/observe", {})
        if st != 200 or not isinstance(obs, dict):
            time.sleep(1.0)
            continue
        if obs.get("done"):
            break
        for p in obs.get("pending") or []:
            key = (p["game"], p["decision"], len(p["view"].get("log") or []))
            n = attempts.get(key, 0)
            attempts[key] = n + 1
            try:
                act = decide(p, seed)
            except Exception as err:
                print("decide error %s: %s" % (key, err), file=sys.stderr)
                act = None
            if act is None or n >= 3:
                act = {"game": p["game"], "decision": p["decision"]}
                if p["decision"] == "speak":
                    act = {"game": p["game"], "decision": "speak", "pass": True}
                else:
                    act["target"] = None
            post(env_url + "/act", act)
    return 0


if __name__ == "__main__":
    sys.exit(main())
