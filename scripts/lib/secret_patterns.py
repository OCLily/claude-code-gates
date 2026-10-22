#!/usr/bin/env python3
"""Secret-Erkennungsmuster — gemeinsame Quelle fuer Gate-Schicht und Sweep.

Dieses Modul ist der vorgesehene SSOT. Die Muster sind WORTGLEICH aus
scripts/commit-secret-sweep.py uebernommen (HIGH_CONF, GENERIC,
looks_generic_secret und deren Schwellen) — kein zweiter, selbstverfasster
Mustersatz. Ein eigener Satz waere der typische Fehler einer selbstverfassten Liste:
eine selbstverfasste Blacklist faengt
genau das, was ihr Autor sich vorgestellt hat, und nichts darueber hinaus.

OFFENER PUNKT: commit-secret-sweep.py haelt die Muster bewusst weiterhin selbst.
Sein Docstring begruendet die Eigenstaendigkeit ausdruecklich ("zwei Guards
duerfen nicht an einem gemeinsamen Import gemeinsam sterben"). Solange das so
bleibt, existieren die Muster an zwei Stellen;
der Selftest von gate_log.py prueft einseitig, ob jedes HIGH_CONF-Muster und die
GENERIC-Schwellen dieses Moduls im Quelltext des Sweeps vorkommen, und schlaegt
sonst fehl (Funktionsrumpf-Vergleich und ein nur im Sweep ergaenztes Muster sind
nicht abgedeckt). Sobald der Sweep
`from secret_patterns import HIGH_CONF, ...` benutzt, ist der Vergleich obsolet.

Was diese Muster NICHT fangen (Maskierung nach Mustern ist prinzipiell
unvollstaendig — diese Liste ist der ehrliche Teil davon):
  * Passwoerter und Passphrasen in natuerlicher Sprache ("hunter2", "Sommer2026!")
    — zu kurz, zu niedrige Entropie, nicht von Prosa unterscheidbar.
  * Secrets mit Trenn- oder Sonderzeichen im Rumpf, die keinen bekannten
    Vendor-Prefix tragen: GENERIC verlangt einen ununterbrochenen alnum-Lauf
    (>=24), ein base64 mit "+/=" oder ein JWT mit Punkten faellt heraus.
  * Mehrzeilige PEM-Bloecke: es gibt KEIN "-----BEGIN"-Muster. HIGH_CONF kennt
    es nicht, und eine Maskierung pro Zeile koennte es auch nicht als Block
    erkennen. Ein Private Key im Kommando wird hoechstens in Teilen getroffen.
  * Vendor-Formate, die der Sweep nicht kennt (alles was nicht in HIGH_CONF
    steht und die GENERIC-Form verletzt), sowie kurze Tokens (<24 Zeichen) ohne
    Prefix.
  * Indirekte Preisgabe: Hostnames, interne URLs, Pfade, Dateinamen,
    Kontonummern, Mailadressen — bewusst kein Ziel.
  * Umgekehrt: GENERIC ist eine Heuristik und trifft auch Unschuldiges (lange
    Hashes, Build-IDs, Base62-Fixtures). Darum protokolliert gate_log.py die
    Zahl der Ersetzungen, damit eine Auswertung weiss, dass sie einen
    veraenderten String vor sich hat.
"""

import math
import re
from collections import Counter

GENERIC_MIN_LEN = 24
GENERIC_MIN_ENTROPY = 3.8
GENERIC_MIN_TRANSITIONS = 3

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
    (re.compile(r"(?<![A-Za-z0-9_])re_(?=[A-Za-z0-9_]{20,})(?=[A-Za-z0-9_]*[A-Z])"
                r"(?=[A-Za-z0-9_]*[0-9])[A-Za-z0-9]+(?:_[A-Za-z0-9]+)?(?![A-Za-z0-9_])"),
     "resend"),
    (re.compile(r"\b[0-9a-fA-F]{32}\b"), "hex32 (Mailgun/MD5-Klasse)"),
    (re.compile(r"\b[0-9a-fA-F]{64}\b"), "hex64"),
]
GENERIC = re.compile(r"(?<![A-Za-z0-9])[A-Za-z0-9]{%d,}(?![A-Za-z0-9])" % GENERIC_MIN_LEN)


def entropy(s):
    n = len(s)
    return -sum((c / n) * math.log2(c / n) for c in Counter(s).values())


def digit_letter_transitions(s):
    return sum(1 for a, b in zip(s, s[1:]) if a.isdigit() != b.isdigit())


def looks_generic_secret(tok):
    return (len(tok) >= GENERIC_MIN_LEN
            and any(c.islower() for c in tok) and any(c.isupper() for c in tok)
            and any(c.isdigit() for c in tok)
            and entropy(tok) >= GENERIC_MIN_ENTROPY
            and digit_letter_transitions(tok) >= GENERIC_MIN_TRANSITIONS)
