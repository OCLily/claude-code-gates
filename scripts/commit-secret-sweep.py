#!/usr/bin/env python3
# PreToolUse hook for Bash: before `git commit`, takes secret-like values removed in the staged diff and
# searches the worktree and index (git grep) for copies that survive elsewhere. It only ever asks, never
# denies; it is standalone with its own pattern copy, and asks when it cannot run (fail-closed).
"""commit-secret-sweep.py — PreToolUse Bash-Gate: Downstream-Sweep vor `git commit`.

Hintergrund: wird ein Secret an der geflaggten Stelle entfernt, kann derselbe
Klartext-Wert in einer zweiten Datei weiterleben. "Geflaggte Stelle korrigiert"
ist nicht "Fall geschlossen". Daraus folgt: jede Secret-Remediation braucht einen
grep des ROHWERTS ueber das GESAMTE Repo, bevor der Fix als fertig gilt.

Dieser Hook macht daraus einen Zahn: jede ENTFERNTE Zeile im gestageten Diff wird nach
Secret-Kandidaten durchsucht; jeder Kandidat wird per `git grep -F` gegen Worktree UND
Index geprueft. Ueberlebt der Wert irgendwo, -> permissionDecision "ask" mit file:line.
"ask" statt "deny", weil es legitime Faelle gibt (Testfixture, dokumentierter Dummy) —
die Entscheidung gehoert zum Maintainer, die Sichtbarkeit zum Hook.

Fail-closed (ein Gate, das nicht laeuft, gilt als nicht bestanden): laesst sich Repo
oder Diff nicht aufloesen, ODER reisst das Zeitbudget, ODER gibt es mehr Kandidaten als
geprueft werden — dann "ask" mit dem Grund, statt still durchzuwinken. Der Rohwert wird
NIE gedruckt, nur maskiert + file:line.

Gesamt-Deadline statt Per-Call-Timeout: der Harness killt den Hook nach `timeout`
Sekunden OHNE Ausgabe — dann kommt kein fail-closed-ask mehr durch, der Commit laeuft
still durch. Das Budget hier liegt bewusst UNTER dem Hook-Timeout, damit die eigene
Meldung noch gedruckt wird.

Kill-Switch fuer Notfaelle: COMMIT_SECRET_SWEEP=off. Kein Netz; geschrieben wird nur das Entscheidungs-Log (gate_log).

Bewusst eigenstaendig statt ueber ein gemeinsames Modul: zwei Guards duerfen
nicht an einem gemeinsamen Import gemeinsam sterben.
"""
import json
import math
import os
import re
import shlex
import subprocess
import sys
import time
from collections import Counter

TOTAL_BUDGET = 9.0
MAX_CALL = 4.0
MAX_TOKENS = 25
MAX_HITS_PER_TOKEN = 3
GENERIC_MIN_LEN = 24
GENERIC_MIN_ENTROPY = 3.8
GENERIC_MIN_TRANSITIONS = 3

VALUE_FLAGS = {"-m", "--message", "-F", "--file", "-c", "--reedit-message",
               "-C", "--reuse-message", "--author", "--date", "-t", "--template",
               "--cleanup", "--squash", "--fixup", "--trailer", "--gpg-sign",
               "-S", "-u", "--untracked-files", "--pathspec-from-file"}
WORKTREE_FLAGS = {"-a", "--all", "-i", "--include", "-o", "--only"}
SEGMENT_SPLIT = re.compile(r"&&|\|\||[;|\n\r]")
ASSIGN = re.compile(r"\A([A-Za-z_][A-Za-z0-9_]*)=(.*)\Z", re.S)
VARREF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}"
                   r"|\$([A-Za-z_][A-Za-z0-9_]*)")

HIGH_CONF = [
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{20,}"), "anthropic"),
    (re.compile(r"sk-[A-Za-z0-9]{20,}"), "openai-style"),
    (re.compile(r"sk_(?:live|test)_[A-Za-z0-9]{16,}"), "stripe"),
    (re.compile(r"rk_(?:live|test)_[A-Za-z0-9]{16,}"), "stripe-restricted"),
    (re.compile(r"AKIA[0-9A-Z]{16}"), "aws-access-key-id"),
    (re.compile(r"ASIA[0-9A-Z]{16}"), "aws-session-key-id"),
    (re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"), "github-token"),
    (re.compile(r"github_pat_[A-Za-z0-9_]{20,}"), "github-pat"),
    (re.compile(r"xox[abposr]-[A-Za-z0-9\-]{10,}"), "slack"),
    (re.compile(r"AIza[0-9A-Za-z_\-]{35}"), "google-api"),
    (re.compile(r"ya29\.[A-Za-z0-9_\-]{20,}"), "google-oauth"),
    (re.compile(r"glpat-[A-Za-z0-9_\-]{16,}"), "gitlab-pat"),
    (re.compile(r"hf_[A-Za-z0-9]{30,}"), "huggingface"),
    (re.compile(r"npm_[A-Za-z0-9]{30,}"), "npm"),
    # Wortgrenze vor re_, Suffix muss Key-Shape haben (Grossbuchstabe + Ziffer,
    # hoechstens EIN Underscore): das nackte re_[A-Za-z0-9_]{20,} lieferte bei
    # echtem Code viele HIGH_CONF-Treffer — snake_case-Identifier und deutsche
    # Prosa. Alert-Fatigue ist der Mechanismus, ueber den ein echter Treffer
    # durchrutscht. Preis: ein Resend-Key voellig ohne Ziffer (selten) entgeht.
    (re.compile(r"(?<![A-Za-z0-9_])re_(?=[A-Za-z0-9_]{20,})(?=[A-Za-z0-9_]*[A-Z])"
                r"(?=[A-Za-z0-9_]*[0-9])[A-Za-z0-9]+(?:_[A-Za-z0-9]+)?(?![A-Za-z0-9_])"),
     "resend"),
    (re.compile(r"\b[0-9a-fA-F]{32}\b"), "hex32 (Mailgun/MD5-Klasse)"),
    (re.compile(r"\b[0-9a-fA-F]{64}\b"), "hex64"),
]
# Ohne _ und - : Identifier/Dateinamen/Satzteile nutzen sie, base62-Secrets nicht.
# Secrets MIT Trennzeichen (github_pat_, glpat-, xox*-) deckt HIGH_CONF praezise ab.
GENERIC = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{%d,}(?![A-Za-z0-9])" % GENERIC_MIN_LEN)

# Kandidaten-Extraktion ueberspringen: Lockfiles/Minified/Notebooks sind
# Pruefsummen- und Base64-Fabriken (FP-Quelle), nicht Secret-Traeger.
SKIP_SRC = re.compile(
    r"(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock|Cargo\.lock|"
    r"composer\.lock|go\.sum|Gemfile\.lock|\.min\.(js|css)|\.ipynb|\.map)$")

# Python-Lockfiles bewusst NICHT in SKIP_SRC: requirements.txt traegt legitim
# `--extra-index-url https://user:pass@host` und Vendor-Tokens, ein
# Datei-Skip waere ein echtes Loch. Stattdessen feiner Schnitt — nur die
# Digest-Klassen, nur auf Zeilen, die eine algorithmus-prefixte Pruefsumme
# tragen. Beide Bedingungen muessen gelten.
# Grund: bei einem pip-compile-Relock einer requirements.lock stehen viele
# `--hash=sha256:`-Zeilen im entfernten Diff; der Digest lebt naturgemaess in
# der requirements.txt weiter und wuerde als `secret-survives` gemeldet.
DEP_LOCK_SRC = re.compile(
    r"(requirements[^/]*\.(txt|lock|in)|constraints[^/]*\.txt|"
    r"uv\.lock|pdm\.lock)$")
# Objekt-anchored (die Digest-Form selbst), nicht verb-anchored: faengt
# pip (`--hash=sha256:`), uv/pdm (`hash = "sha256:"`) und poetry gleichermassen,
# ohne je Schreibweise einzeln zu erraten.
DEP_HASH_LINE = re.compile(r"\b(?:sha256|sha512|sha384|md5):[0-9a-fA-F]{32,}\b")
# Nur diese Labels werden unterdrueckt. Vendor-Prefixe (ghp_, AKIA, sk-ant-…)
# und GENERIC bleiben in DIESEN Dateien voll aktiv.
HASH_LABELS = {"hex64", "hex32 (Mailgun/MD5-Klasse)"}

_deadline = [0.0]


class Budget(Exception):
    pass


def git(args, cwd):
    left = _deadline[0] - time.monotonic()
    if left <= 0.3:
        raise Budget(f"Zeitbudget aufgebraucht vor `git {' '.join(args[:2])}`")
    return subprocess.run(["git"] + args, cwd=cwd, capture_output=True,
                          text=True, timeout=min(MAX_CALL, left))


def expand(raw, env):
    """`cd "$T"` textuell aufloesen. Der Hook sieht den
    Kommandotext VOR der Shell-Expansion; ohne diese Aufloesung baute er aus
    `cd "$T"` ein cwd="<base>/$T" und stuerbe mit FileNotFoundError, statt den
    Sweep zu fahren. Nur literale Zuweisungen aus demselben Kommando,
    keine Shell, kein Environment des Hooks. So sweepen viele dieser Faelle
    wirklich; einige bleiben strukturell unsweepbar (das Repo wird vom SELBEN
    Kommando erst angelegt, der Hook laeuft davor), andere haengen an einem
    Ausdruck, den nur die Shell kennt ($(date), $1)."""
    return VARREF.sub(lambda m: env.get(m.group(1) or m.group(2), m.group(0)), raw)


def parse(cmd):
    """(chdir, pathspecs, worktree_mode, is_commit). Segmente auch an Newlines —
    ein mehrzeiliger Bash-Call war sonst ein stiller Bypass des ganzen Gates."""
    chdir, env, git_env = None, {}, {}
    for seg in SEGMENT_SPLIT.split(cmd):
        fuzzy = "$(" in seg or "`" in seg
        try:
            toks = shlex.split(seg)
        except ValueError:
            toks, fuzzy = seg.split(), True
        if not toks:
            continue
        while toks and toks[0] in ("export", "declare", "typeset", "local"):
            toks = toks[1:]
        assigned = []
        while toks:
            hit = ASSIGN.match(toks[0])
            if not hit:
                break
            env[hit.group(1)] = expand(hit.group(2), env)
            assigned.append(hit.group(1))
            toks = toks[1:]
        # Zuweisungen zaehlen fuer das Commit nur, wenn sie allein stehen
        # (`export GIT_DIR=/x;`) oder Prefix EINES git-Aufrufs sind. Als Prefix
        # eines fremden Kommandos (`GIT_DIR=/x ls`) leben sie in der Shell nicht
        # weiter und duerfen das Sweep-Ziel nicht verschieben.
        if not toks or toks[0] == "git":
            for k in assigned:
                git_env[k] = env[k]
        if not toks:
            continue
        if toks[0] in ("cd", "pushd") and len(toks) > 1:
            chdir = expand(toks[1], env)
            continue
        if toks[0] != "git":
            continue
        # Das Assignment-Stripping macht `GIT_DIR=/x git commit` ueberhaupt
        # erst als Commit sichtbar (das Zuweisungs-Token wuerde sonst den
        # Kopf verdecken). Ohne diese Zeilen wuerde `base` gesweept — das FALSCHE
        # Repo, Ergebnis allow/no-candidate, also ein stiller Durchlass. Reihenfolge wie
        # bei git selbst: ein explizites -C/--git-dir= im Flag-Loop ueberschreibt.
        env_target = git_env.get("GIT_WORK_TREE") or git_env.get("GIT_DIR")
        if env_target:
            chdir = env_target
        i = 1
        while i < len(toks) and toks[i].startswith("-"):
            if toks[i] == "-C" and i + 1 < len(toks):
                chdir = expand(toks[i + 1], env)
                i += 2
            elif toks[i].startswith("--git-dir=") or toks[i].startswith("--work-tree="):
                chdir = expand(toks[i].split("=", 1)[1], env)
                i += 1
            elif toks[i] == "-c" and i + 1 < len(toks):
                i += 2
            else:
                i += 1
        if i >= len(toks) or toks[i] != "commit":
            continue
        if fuzzy:
            return chdir, [], True, True
        rest, paths, worktree, after_ddash = toks[i + 1:], [], False, False
        j = 0
        while j < len(rest):
            t = rest[j]
            if after_ddash:
                paths.append(t)
            elif t == "--":
                after_ddash = True
            elif t in WORKTREE_FLAGS:
                worktree = True
            elif t in VALUE_FLAGS:
                j += 1
            elif re.fullmatch(r"-[a-zA-Z]{2,}", t):
                if "a" in t[1:]:
                    worktree = True
                if any(c in t[1:] for c in "mFcCStu"):
                    j += 1
            elif not t.startswith("-"):
                paths.append(t)
            j += 1
        return chdir, paths, worktree, True
    return chdir, [], False, False


def entropy(s):
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def digit_letter_transitions(s):
    """Zufalls-Secrets streuen Ziffern; Identifier klumpen sie ans Ende (Handler123)."""
    return sum(1 for a, b in zip(s, s[1:]) if a.isdigit() != b.isdigit())


def looks_generic_secret(tok):
    return (len(tok) >= GENERIC_MIN_LEN
            and any(c.islower() for c in tok) and any(c.isupper() for c in tok)
            and any(c.isdigit() for c in tok)
            and entropy(tok) >= GENERIC_MIN_ENTROPY
            and digit_letter_transitions(tok) >= GENERIC_MIN_TRANSITIONS)


def diff_file(line):
    """Dateiname aus einer +++-Zeile. `+++ /dev/null` (geloeschte Datei) und
    gequotete Pfade (`+++ "b/mit leerzeichen.py"`) muessen mit — eine geloeschte
    Secret-Datei ist der haeufigste Remediation-Move und darf den Dateikontext
    nicht auf der vorigen (evtl. SKIP_SRC-)Datei stehen lassen."""
    rest = line[4:].strip()
    if rest == "/dev/null":
        return ""
    if rest.startswith('"') and rest.endswith('"') and len(rest) > 1:
        rest = rest[1:-1]
    if rest.startswith("b/"):
        rest = rest[2:]
    return rest


def old_file_header(line, next_line):
    """Echter `---`-Dateikopf vs. ENTFERNTE Inhaltszeile, die mit `--` beginnt.

    Wuerde jede Zeile mit `---`-Prefix als Kopf verworfen, waere eine entfernte
    `--extra-index-url https://user:<token>@host` unsichtbar: sie erscheint im
    Unified-Diff als `---extra-index-url …` — genau die Form, die ein Credential
    in einer Python-Dependency-Datei traegt.

    Zwei Eigenschaften, verifiziert gegen echte `git diff -U0`-Ausgabe fuer
    Modify/Add/Delete (inkl. beider /dev/null-Seiten): der Kopf hat ein
    Leerzeichen nach den drei Strichen UND steht ausnahmslos unmittelbar vor der
    `+++ `-Zeile. Beide Bedingungen sind tragend: das Leerzeichen allein wuerde
    eine entfernte SQL-Zeile `-- comment` (-> `--- comment`) weiter verwerfen,
    der Nachbar allein wuerde eine normale entfernte Zeile direkt vor einem
    `+++ `-Kopf verwerfen."""
    return line.startswith("--- ") and next_line.startswith("+++ ")


def candidates(diff_text):
    """Secret-Kandidaten aus ENTFERNTEN Zeilen: {token: klasse}, Reihenfolge stabil."""
    found, cur_file = {}, ""
    lines = diff_text.splitlines()
    for i, line in enumerate(lines):
        if line.startswith("+++ "):
            cur_file = diff_file(line)
            continue
        if not line.startswith("-"):
            continue
        if old_file_header(line, lines[i + 1] if i + 1 < len(lines) else ""):
            continue
        if cur_file and SKIP_SRC.search(cur_file):
            continue
        body = line[1:]
        hash_noise = bool(cur_file and DEP_LOCK_SRC.search(cur_file)
                          and DEP_HASH_LINE.search(body))
        for rx, label in HIGH_CONF:
            for tok in rx.findall(body):
                if hash_noise and label in HASH_LABELS:
                    continue
                found.setdefault(tok, label)
        for tok in GENERIC.findall(body):
            if tok not in found and looks_generic_secret(tok):
                found[tok] = "generisch (hohe Entropie)"
    return found


def drop_git_objects(found, cwd):
    """40/64-stellige Hex, die ein echtes git-Objekt ist, ist ein SHA — kein Secret.
    Greift praktisch nur fuer hex64: reine Lowercase-hex40 matcht weder HIGH_CONF
    (nur 32/64) noch GENERIC (verlangt Gross+Klein). Bleibt als Netz fuer
    mixed-case-Faelle, kostet einen budgetierten git-Call je Kandidat."""
    out = {}
    for tok, label in found.items():
        if len(tok) in (40, 64) and re.fullmatch(r"[0-9a-fA-F]+", tok):
            if git(["cat-file", "-e", tok + "^{object}"], cwd).returncode == 0:
                continue
        out[tok] = label
    return out


def grep_tree(tokens, cwd):
    """{token: [file:line, ...]} ueber Worktree UND Index, ein git-grep-Aufruf je Scope."""
    hits = {t: [] for t in tokens}
    args = []
    for t in tokens:
        args += ["-e", t]
    for scope in ([], ["--cached"]):
        r = git(["grep", "-n", "-I", "--fixed-strings"] + scope + args, cwd)
        if r.returncode not in (0, 1):
            continue
        tag = "" if scope == [] else "index:"
        for line in r.stdout.splitlines():
            parts = line.split(":", 2)
            if len(parts) < 3:
                continue
            plain = f"{parts[0]}:{parts[1]}"
            for t in tokens:
                if t not in parts[2]:
                    continue
                if plain in hits[t] or f"index:{plain}" in hits[t]:
                    continue
                hits[t].append(tag + plain)
    return hits


def resolve_cwd(chdir, base):
    """(cwd, aufloesbar). Nicht aufloesbar = unbekannte Variable, Backtick/$(…)
    oder kein Verzeichnis: dann kann der Sweep das Repo nicht sehen und sagt das,
    statt mit FileNotFoundError als "KAPUTT" zu melden."""
    cwd = os.path.expanduser(chdir) if chdir else base
    if not os.path.isabs(cwd):
        cwd = os.path.join(base, cwd)
    ok = "$" not in cwd and "`" not in cwd and os.path.isdir(cwd)
    return cwd, ok


def mask(tok):
    return f"{tok[:4]}…{tok[-4:]} ({len(tok)} Zeichen)"


# Entscheidungs-Protokoll (metrics/gates). Fail-open und bewusst defensiv
# importiert: ein fehlender oder kaputter Logger darf dieses Gate nie zum
# Absturz bringen -- ein abgestuerzter PreToolUse-Hook sperrt im schlimmsten
# Fall jeden Bash-Aufruf aus.
GATE_LOG_NAME = "commit-secret-sweep"
# Der Selftest darf die Messung nicht verschmutzen -- er faehrt praeparierte
# Payloads durch dieselbe Datei, teils als Subprozess (erbt das Env).
_SWEEP_ENV = os.environ.get("COMMIT_SECRET_SWEEP", "")
if "--selftest" in sys.argv:
    os.environ["CLAUDE_GATE_LOG_DISABLE"] = "1"
    # Steht der Kill-Switch auf off, beenden sich die e2e-Subprozesse (die das Env
    # erben) in Zeile 2 von main() still — vier Fehler mit irrefuehrender Ursache.
    os.environ["COMMIT_SECRET_SWEEP"] = "on"
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
try:
    from gate_log import log_decision
except Exception:
    def log_decision(*_args, **_kwargs):
        pass


def ask(reason, *, key="sweep-not-executable"):
    """Emittiert IMMER `ask`, nie `deny` -- und das ist eine bekannte Grenze.

    In jedem Auto-Approve-Modus (`bypassPermissions`, `acceptEdits`, `dontAsk`)
    greift ausschliesslich `deny`; ein `ask` ist dort dekorativ und der Commit
    laeuft durch, ohne dass der Sweep gelaufen ist. `destructive-command-gate`
    eskaliert dafuer modusabhaengig (`"ask" if mode in ("default","plan") else
    "deny"`), dieses Gate nicht.

    Nicht stillschweigend geaendert, weil es eine Policy-Frage ist: die
    `target-dir-unresolved`-Klasse (Zielverzeichnis nur als Shell-Ausdruck, etwa
    `cd "$T"`) ist ein Alltagsfall, ein `deny` dort wuerde also der dominante
    Ausgang und nicht der Randfall.
    """
    log_decision(GATE_LOG_NAME, "ask", key, _payload[0])
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "ask",
        "permissionDecisionReason": reason}}))
    sys.exit(0)


_payload = [{}]


def main():
    _deadline[0] = time.monotonic() + TOTAL_BUDGET
    if os.environ.get("COMMIT_SECRET_SWEEP", "").lower() == "off":
        sys.exit(0)
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    cmd = (data.get("tool_input") or {}).get("command", "")
    if not cmd or not re.search(r"\bgit\b[^;&|\n\r]*\bcommit\b", cmd):
        sys.exit(0)
    chdir, paths, worktree, is_commit = parse(cmd)
    if not is_commit:
        sys.exit(0)
    _payload[0] = data

    base = data.get("cwd") or os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd()
    cwd, resolvable = resolve_cwd(chdir, base)
    if not resolvable:
        ask(f"[commit-secret-sweep] Ziel-Verzeichnis des Commits nicht aufloesbar "
            f"(cwd={cwd}) — der Pfad steht nur als Shell-Ausdruck im Kommando oder "
            f"existiert nicht. Der Downstream-Sweep ist NICHT gelaufen, dieser "
            f"Commit ist also UNGEPRUEFT. Das hier ist ein `ask`, kein `deny`: in "
            f"einem Auto-Approve-Modus laeuft der Commit trotzdem durch. Entweder "
            f"mit absolutem Pfad committen (`git -C /abs/pfad commit`) oder vor dem "
            f"Commit selbst: git grep -nF '<rohwert>'.",
            key="target-dir-unresolved")

    overflow = 0
    try:
        top = git(["rev-parse", "--show-toplevel"], cwd)
        if top.returncode != 0:
            ask(f"[commit-secret-sweep] Repo nicht aufloesbar (cwd={cwd}): "
                f"{top.stderr.strip()[:200]}. Gate nicht ausfuehrbar = nicht bestanden "
                f"(Regel des Setups). Manuell pruefen oder bewusst bestaetigen.",
                key="repo-unresolved")
        root = top.stdout.strip()
        if paths:
            diff_args = ["diff", "HEAD", "--"] + paths
        elif worktree:
            diff_args = ["diff", "HEAD"]
        else:
            diff_args = ["diff", "--cached"]
        d = git(diff_args + ["-U0"], root)
        if d.returncode != 0:
            ask(f"[commit-secret-sweep] Diff fehlgeschlagen: {d.stderr.strip()[:200]}. "
                f"Gate nicht ausfuehrbar = nicht bestanden. Manuell pruefen.")
        found = drop_git_objects(candidates(d.stdout), root)
        if not found:
            log_decision(GATE_LOG_NAME, "allow", "no-candidate", data)
            sys.exit(0)
        overflow = max(0, len(found) - MAX_TOKENS)
        tokens = list(found)[:MAX_TOKENS]
        hits = grep_tree(tokens, root)
    except (subprocess.TimeoutExpired, Budget) as e:
        ask(f"[commit-secret-sweep] Zeitbudget/Timeout ({type(e).__name__}) — der "
            f"Downstream-Sweep ist NICHT vollstaendig gelaufen. Gate nicht ausfuehrbar = "
            f"nicht bestanden. Vor dem Commit selbst: git grep -nF '<rohwert>' — oder "
            f"bewusst ueberspringen mit COMMIT_SECRET_SWEEP=off.")
    except Exception as e:
        ask(f"[commit-secret-sweep] KAPUTT ({type(e).__name__}: {e}) — Sweep NICHT gelaufen. "
            f"Gate nicht ausfuehrbar = nicht bestanden. Guard reparieren.")

    surviving = {t: h for t, h in hits.items() if h}
    if not surviving and not overflow:
        log_decision(GATE_LOG_NAME, "allow", "no-survivor", data)
        sys.exit(0)
    lines = []
    for t, h in surviving.items():
        lines.append(f"  {mask(t)} [{found[t]}] lebt weiter in: "
                     + ", ".join(h[:MAX_HITS_PER_TOKEN])
                     + (f" (+{len(h) - MAX_HITS_PER_TOKEN} weitere)"
                        if len(h) > MAX_HITS_PER_TOKEN else ""))
    if overflow:
        lines.append(f"  ACHTUNG: {overflow} weitere Kandidaten wurden NICHT geprueft "
                     f"(Limit {MAX_TOKENS}). Fuer diese sagt der Hook NICHTS aus — "
                     f"Massen-Redaction manuell sweepen.")
    ask("[commit-secret-sweep] Dieser Commit ENTFERNT einen Secret-verdaechtigen Wert, der "
        "anderswo im Repo WEITERLEBT — die geflaggte Stelle zu korrigieren genuegt "
        "nicht, der Rohwert muss im ganzen Repo weg:\n"
        + "\n".join(lines)
        + f"\nEntscheiden: (a) auch dort entfernen, dann erneut committen; (b) bewusst "
          f"bestaetigen, wenn es ein Dummy/Testfixture ist.\nHistorie separat pruefen — "
          f"dieser Hook sieht NUR Worktree+Index, nicht vergangene Commits: "
          f"git -C {root} log -S'<rohwert>' --oneline --all",
        key="secret-survives")


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        import tempfile
        ok = subprocess.run(["git", "--version"], capture_output=True).returncode == 0
        nl = chr(10)
        probe = "AKIAIOSFODNN7EXAMPLE"
        c = candidates("+++ b/x.py" + nl + "-KEY = '" + probe + "'" + nl)
        # geloeschte Datei nach einer Lockfile: cur_file darf nicht haengenbleiben
        c2 = candidates("+++ b/go.sum" + nl + "-x" + nl + "--- a/s.py" + nl
                        + "+++ /dev/null" + nl + "-KEY = '" + probe + "'" + nl)
        cases = {
            "plain": ("git commit -m x", (None, [], False, True)),
            "newline": ("git add f" + nl + "git commit -m x", (None, [], False, True)),
            "cd-newline": ("cd /r" + nl + "git commit -m x", ("/r", [], False, True)),
            "dash-a": ("git -c u=v commit -a -m y", (None, [], True, True)),
            "dash-am": ("git commit -am y", (None, [], True, True)),
            "no-commit": ("echo git commit", (None, [], False, False)),
            # Variablen-Ziel: der Sweep muss laufen, statt an einem Pfad
            # "<base>/$T" mit FileNotFoundError zu sterben.
            "var-cd": ('T=/private/tmp/x; cd "$T"' + nl + "git commit -m y",
                       ("/private/tmp/x", [], False, True)),
            "var-export": ("export T=/private/tmp/x; cd $T; git commit -m y",
                           ("/private/tmp/x", [], False, True)),
            "var-braces": ("T=/private/tmp/x; git -C ${T} commit -m y",
                           ("/private/tmp/x", [], False, True)),
            "var-nested": ("B=/private/tmp; T=$B/x; cd $T; git commit -m y",
                           ("/private/tmp/x", [], False, True)),
            "var-gitC": ("R=/a/b; git -C $R add f && git -C $R commit -m y",
                         ("/a/b", [], False, True)),
            "var-prefix-env": ("T=/a/b git commit -m y", (None, [], False, True)),
            # GIT_DIR/GIT_WORK_TREE sind Repo-Wahl, nicht Dekoration. Beidseitig:
            # uebernommen als Prefix/export, NICHT als Prefix eines Fremdkommandos.
            "git-dir-env": ("GIT_DIR=/a/b git commit -m y", ("/a/b", [], False, True)),
            "git-work-tree-export": ("export GIT_WORK_TREE=/a/b; git commit -m y",
                                     ("/a/b", [], False, True)),
            "git-work-tree-beats-git-dir": ("GIT_DIR=/a GIT_WORK_TREE=/b git commit -m y",
                                            ("/b", [], False, True)),
            "dash-C-beats-git-dir-env": ("GIT_DIR=/a git -C /b commit -m y",
                                         ("/b", [], False, True)),
            "git-dir-env-foreign-cmd-ignored": ("GIT_DIR=/a ls; git commit -m y",
                                                (None, [], False, True)),
            # Unbekannte Variable bleibt stehen -> resolve_cwd sagt "nicht
            # aufloesbar". Kein erfundener Pfad, kein "KAPUTT".
            "var-unknown": ("cd $NOPE; git commit -m y", ("$NOPE", [], False, True)),
            # shlex scheitert am mehrzeiligen $(printf ...): ohne den fuzzy-Zweig
            # wuerde Prosa zu Pathspecs, `git diff` schluege fehl -> gar kein Sweep.
            "fuzzy-printf": ("cd /r && git commit -m \"$(printf 'fix(x): a" + nl
                             + "b: c')\"", ("/r", [], True, True)),
            # Anfuehrungszeichen IN der Nachricht: shlex wirft nicht, tokenisiert
            # aber Prosa -> ohne den fuzzy-Zweig wuerden daraus Pathspecs.
            "subst-quote-in-message": (
                "cd /r && git commit -m \"$(printf 'a: 1. \"Offene\" b')\"",
                ("/r", [], True, True)),
            "subst-backtick": ("git commit -m \"`date`\"", (None, [], True, True)),
            # Durchlass-Seite: echte Pathspecs bleiben Pathspecs.
            "pathspec-kept": ("git commit -m x -- a.py b.py",
                              (None, ["a.py", "b.py"], False, True)),
            "assign-no-commit": ("T=/a; echo $T", (None, [], False, False)),
        }
        bad = [k for k, (cm, want) in cases.items() if parse(cm) != want]
        # Eigenes Temp-Verzeichnis statt eines uid-spezifischen Session-Artefakts:
        # /private/tmp/claude-501 verschwindet bei Cleanup/Reboot und existiert fuer
        # einen anderen Nutzer nie — ein Gate-Test darf daran nicht haengen.
        tmp = tempfile.mkdtemp(prefix="csw-selftest-")
        tmp_parent, tmp_name = os.path.dirname(tmp), os.path.basename(tmp)
        cwd_cases = {
            "cwd-abs-exists": ((tmp, "/base"), (tmp, True)),
            "cwd-none": ((None, tmp), (tmp, True)),
            "cwd-var": (("$T", tmp), (os.path.join(tmp, "$T"), False)),
            "cwd-backtick": (("`pwd`", tmp), (os.path.join(tmp, "`pwd`"), False)),
            "cwd-missing": ((os.path.join(tmp, "kein-verzeichnis-xyz"), "/base"),
                            (os.path.join(tmp, "kein-verzeichnis-xyz"), False)),
            "cwd-rel-exists": ((tmp_name, tmp_parent), (tmp, True)),
        }
        bad += [k for k, (args, want) in cwd_cases.items()
                if resolve_cwd(*args) != want]

        # Block- UND Durchlass-Seite durch DIESELBE Datei als Prozess: eine
        # bestandene Durchlass-Probe belegt die Block-Seite nicht.
        def run(command, *, ceiling=None):
            payload = {"tool_input": {"command": command}, "cwd": tmp,
                       "permission_mode": "bypassPermissions",
                       "session_id": "selftest", "tool_name": "Bash"}
            env = dict(os.environ)
            if ceiling:
                # Deckel fuer die .git-Suche nach oben: der Fall unten verlaesst sich
                # darauf, dass `tmp` KEIN Repo ist. Laege TMPDIR in einem Repo, wuerde
                # der Test sonst von der Umgebung abhaengen.
                env["GIT_CEILING_DIRECTORIES"] = ceiling
            return subprocess.run([sys.executable, os.path.abspath(__file__)],
                                  input=json.dumps(payload), capture_output=True,
                                  text=True, timeout=20, env=env).stdout.strip()

        def decision(out):
            if not out:
                return None
            return json.loads(out)["hookSpecificOutput"]["permissionDecision"]

        unres = run("cd $NOPE; git commit -m y")
        missing = run("cd " + os.path.join(tmp, "kein-verzeichnis-xyz")
                      + " && git commit -m y")
        # Der EIGENTLICHE Zweck der Variablenaufloesung, nicht nur ihre Block-Seite: eine
        # aufgeloeste Variable muss durch resolve_cwd HINDURCH und der Fluss
        # dahinter weiterlaufen. `tmp` existiert und ist KEIN Repo -> der Befund
        # muss "Repo nicht aufloesbar" sein (Schluessel repo-unresolved), nicht
        # "Ziel-Verzeichnis ... nicht aufloesbar" (target-dir-unresolved). Wuerde
        # die Expansion nicht greifen, waere cwd "<base>/$T" und der Fall landete
        # im target-dir-Zweig. Kein Repo wird dafuer angelegt.
        expanded = run("T=" + tmp + "; cd $T; git commit -m y", ceiling=tmp_parent)
        e2e = {
            "e2e-var-dir-asks": (decision(unres) == "ask"
                                 and "nicht aufloesbar" in unres),
            "e2e-missing-dir-asks": (decision(missing) == "ask"
                                     and "nicht aufloesbar" in missing),
            "e2e-expanded-var-reaches-sweep": (
                decision(expanded) == "ask"
                and "Repo nicht aufloesbar" in expanded
                and "Ziel-Verzeichnis" not in expanded),
            "e2e-no-commit-silent": run("echo git commit") == "",
            "e2e-non-git-silent": run("ls -la") == "",
        }
        bad += [k for k, good in e2e.items() if not good]
        os.rmdir(tmp)
        probes = {"git": ok, "regex-probe": probe in c, "deleted-file-probe": probe in c2}
        bad += [k for k, good in probes.items() if not good]

        # Digest-Rauschen in Python-Lockfiles: der feine Schnitt muss BEIDSEITIG
        # halten. Links die Unterdrueckung (sonst Alert-Fatigue),
        # rechts die unveraenderte Durchlass-Seite — ein Datei-Skip waere hier
        # ein Loch, weil requirements.txt legitim Index-Credentials traegt.
        # Synthetische Werte: AWS-Doku-Beispiel bzw. offensichtlicher Dummy.
        digest = "a" * 32 + "b" * 32
        tokenish = "ghp_" + "A1b2C3d4E5f6G7h8J9k0" + "LMN"

        def cand(path, body):
            return candidates("+++ b/" + path + nl + "-" + body + nl)

        hashcut = {
            # (1) der Zielfall: Relock-Zeile wird uebersprungen
            "lock-relock-hash-skipped":
                not cand("requirements.lock", "    --hash=sha256:" + digest + " \\"),
            "lock-txt-hash-skipped":
                not cand("requirements.txt", "pkg==1.0 --hash=sha256:" + digest),
            "uv-lock-hash-skipped":
                not cand("uv.lock", 'hash = "sha256:' + digest + '"'),
            "pdm-lock-hash-skipped":
                not cand("pdm.lock", 'hash = "sha256:' + digest + '"'),
            "nested-path-hash-skipped":
                not cand("sub/dir/requirements-dev.txt",
                         "--hash=sha256:" + digest),
            # (2) Gegenseite A: derselbe Digest in einer NORMALEN Datei bleibt
            # ein Treffer -> der Schnitt ist dateigebunden, nicht global
            "same-digest-in-py-still-found":
                digest in cand("config.py", "SECRET = '" + digest + "'"),
            # (3) Gegenseite B: hex64 OHNE Digest-Prefix in derselben Lockfile
            # bleibt ein Treffer -> der Schnitt ist zeilengebunden
            "lock-hex64-without-digest-prefix-still-found":
                digest in cand("requirements.txt", "TOKEN = '" + digest + "'"),
            # (4) Gegenseite C: die benannte Grenze. Ein Credential in einer
            # Index-/Paket-URL in requirements.txt MUSS weiter gefunden werden —
            # genau deshalb steht die Datei NICHT in SKIP_SRC.
            # (Die Variante `--extra-index-url` am Zeilenanfang hat einen
            # eigenen Pruefblock, siehe `dashdash` unten.)
            "lock-index-url-credential-still-found":
                tokenish in cand("requirements.txt",
                                 "pkg @ https://u:" + tokenish
                                 + "@pypi.example.com/pkg.whl"),
            # (5) Gegenseite D: Vendor-Token auf DERSELBEN Zeile wie ein Digest
            # -> nur die Digest-Klasse ist unterdrueckt, nicht die Zeile
            "vendor-token-on-hash-line-still-found":
                tokenish in cand("requirements.lock",
                                 tokenish + " --hash=sha256:" + digest),
            # (6) echte Lockfiles bleiben beim bestehenden Ganzdatei-Skip
            "true-lockfile-unchanged":
                not cand("go.sum", "h1:" + digest),
        }
        bad += [k for k, good in hashcut.items() if not good]

        # Kopfzeile vs. entfernte `--`-Inhaltszeile. Die GEGENSEITE ist hier die
        # wichtigere: wuerde ein echter Dateikopf als Inhalt gelesen, meldete
        # jeder Commit, der eine Datei mit 32-stelliger Hex-Komponente im Pfad
        # anfasst (Build-/Content-Addressed-Verzeichnisse) — schlimmer als das
        # Loch. Darum traegt jeder Kopf-Fall unten hex32 IM PFAD.
        hexpath = "build/" + "c" * 32 + "/mod.py"
        dashdash = {
            # (1) die Zielform, in beiden realen Spielarten
            "removed-extra-index-url-found":
                tokenish in candidates(
                    "+++ b/requirements.txt" + nl
                    + "---extra-index-url https://u:" + tokenish + "@host" + nl),
            "removed-extra-index-url-in-deleted-file-found":
                tokenish in candidates(
                    "--- a/requirements.txt" + nl + "+++ /dev/null" + nl
                    + "---extra-index-url https://u:" + tokenish + "@host" + nl),
            # (2) `-- ` MIT Leerzeichen (SQL/Lua-Kommentar): belegt, dass das
            # Leerzeichen-Kriterium allein nicht traegt
            "removed-dashdash-space-line-found":
                tokenish in candidates("+++ b/seed.sql" + nl
                                       + "--- token: " + tokenish + nl),
            # (3) belegt, dass der +++-Nachbar allein nicht traegt: normale
            # entfernte Zeile unmittelbar vor einem `+++ `-Kopf
            "plain-removed-line-before-plus-header-found":
                tokenish in candidates("+++ b/a.py" + nl
                                       + "-KEY = '" + tokenish + "'" + nl
                                       + "+++ b/b.py" + nl),
            # (4) Gegenseite: echter Kopf bleibt Kopf, alle drei Spielarten
            "real-header-modify-discarded":
                not candidates("diff --git a/" + hexpath + " b/" + hexpath + nl
                               + "index 1111111..2222222 100644" + nl
                               + "--- a/" + hexpath + nl
                               + "+++ b/" + hexpath + nl
                               + "@@ -1 +1 @@" + nl + "-harmlos" + nl),
            "real-header-delete-discarded":
                not candidates("--- a/" + hexpath + nl + "+++ /dev/null" + nl
                               + "-harmlos" + nl),
            "real-header-add-discarded":
                not candidates("--- /dev/null" + nl + "+++ b/" + hexpath + nl
                               + "+harmlos" + nl),
            # (5) kopf-aehnliche Inhaltszeile als LETZTE Diff-Zeile (kein
            # Nachbar) -> Inhalt, kein Index-Fehler
            "dashdash-at-eof-found":
                tokenish in candidates("+++ b/requirements.txt" + nl
                                       + "---extra-index-url u:" + tokenish + "@h"),
            # (6) Datei-Scoping unberuehrt
            "dashdash-in-skipped-file-still-skipped":
                not candidates("+++ b/go.sum" + nl
                               + "---extra-index-url u:" + tokenish + "@h" + nl),
        }
        bad += [k for k, good in dashdash.items() if not good]

        checked = (len(cases) + len(cwd_cases) + len(e2e) + len(probes)
                   + len(hashcut) + len(dashdash))
        print(f"geprueft={checked} (parse={len(cases)}, resolve_cwd={len(cwd_cases)}, "
              f"e2e={len(e2e)}, probes={len(probes)}, hashcut={len(hashcut)}, "
              f"dashdash={len(dashdash)}), "
              f"fehler={len(bad)}"
              + ("" if not bad else " -> " + ",".join(bad))
              + f"; budget={TOTAL_BUDGET}s; "
              f"sweep-env={_SWEEP_ENV or '(unset)'} (im Selftest auf on gezwungen)")
        sys.exit(1 if bad else 0)
    main()
