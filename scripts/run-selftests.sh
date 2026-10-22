#!/bin/bash
# run-selftests.sh — fuehrt jedes scripts/*.py und scripts/lib/*.py mit --selftest
# aus, gibt je Script eine Zeile plus eine Summenzeile aus und endet mit Exit 1,
# wenn ein Selftest scheitert.
# Umgebung: die Selftests enthalten woertlich Pfade unter /Users/alice (der
# Platzhalter-Home dieses Exports), deshalb laufen sie mit HOME=/Users/alice; das
# Verzeichnis muss nicht existieren. Ausnahme merge-gate.py: es liest ~/.claude und
# bekommt ein Wegwerf-HOME mit einem leeren Git-Repo als ~/.claude. Das darf nicht
# unter /tmp liegen: sechs merge-gate-Faelle nehmen /tmp als "fremdes" Verzeichnis
# und scheitern, wenn ~/.claude darunter liegt. Das Gate-Log ist abgeschaltet
# (CLAUDE_GATE_LOG_DISABLE=1); nur der Selftest von lib/gate_log.py laeuft mit
# aktivem Log und einem eigenen Temp-Log-Verzeichnis. Das Wegwerf-Verzeichnis wird am
# Ende wieder entfernt. Einzige Schreibstelle im Repo ist metrics/selftests/state.json
# (steht in .gitignore): das maschinenlesbare Ergebnis des letzten Laufs.
# Jeder Aufruf: </dev/null (kein stdin-Hang bei fehlerhaftem Script) und, wenn
# timeout/gtimeout vorhanden ist, ein eigenes Zeitlimit je Script.

SCRIPTS_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
BASE_DIR="$(dirname "$SCRIPTS_DIR")"
STATE="$BASE_DIR/metrics/selftests/state.json"
PER_SCRIPT_TIMEOUT=60

BASE_TMP="${TMPDIR:-/var/tmp}"
case "$BASE_TMP" in
  /tmp|/tmp/*|/private/tmp|/private/tmp/*) BASE_TMP=/var/tmp ;;
esac
WORK=$(mktemp -d "${BASE_TMP%/}/gate-selftest.XXXXXX") || exit 2
trap 'rm -r "$WORK"' EXIT
mkdir -p "$WORK/home" "$WORK/gatelog"
git init -q "$WORK/home/.claude" || exit 2

TIMEOUT_CMD=()
TIMEOUT_BIN=$(command -v timeout || command -v gtimeout)
[ -n "$TIMEOUT_BIN" ] && TIMEOUT_CMD=("$TIMEOUT_BIN" "$PER_SCRIPT_TIMEOUT")
[ -n "$TIMEOUT_BIN" ] || echo "WARN: kein timeout/gtimeout gefunden — Selftests laufen ohne Hang-Schutz"

TOTAL=0
FAILED=0
SCRIPTS_JSON="{"
FIRST=1

for f in "$SCRIPTS_DIR"/*.py "$SCRIPTS_DIR"/lib/*.py; do
  [ -f "$f" ] || continue
  grep -l -- '--selftest' "$f" >/dev/null 2>&1 || continue
  # Name relativ zu SCRIPTS_DIR ("lib/gate_log.py"), nicht basename: scripts/x.py
  # und scripts/lib/x.py sollen unterscheidbar bleiben.
  name=${f#"$SCRIPTS_DIR"/}
  TOTAL=$((TOTAL + 1))
  out="$WORK/out.txt"
  home=/Users/alice
  [ "$name" = "merge-gate.py" ] && home="$WORK/home"
  if [ "$name" = "lib/gate_log.py" ]; then
    env -u CLAUDE_GATE_LOG_DISABLE HOME="$home" CLAUDE_GATE_LOG_DIR="$WORK/gatelog" \
      PYTHONDONTWRITEBYTECODE=1 "${TIMEOUT_CMD[@]}" python3 -B "$f" --selftest \
      < /dev/null > "$out" 2>&1
  else
    env HOME="$home" CLAUDE_GATE_LOG_DISABLE=1 \
      PYTHONDONTWRITEBYTECODE=1 "${TIMEOUT_CMD[@]}" python3 -B "$f" --selftest \
      < /dev/null > "$out" 2>&1
  fi
  exit_code=$?
  if [ "$exit_code" -eq 0 ]; then
    echo "ok    $name: $(grep -v '^[[:space:]]*$' "$out" | tail -n 1)"
  else
    FAILED=$((FAILED + 1))
    echo "FAIL  $name (exit=$exit_code), letzte Zeilen:"
    tail -n 15 "$out" | sed 's/^/        /'
  fi
  [ "$FIRST" -eq 1 ] && FIRST=0 || SCRIPTS_JSON="$SCRIPTS_JSON,"
  SCRIPTS_JSON="$SCRIPTS_JSON\"$name\":$exit_code"
done
SCRIPTS_JSON="$SCRIPTS_JSON}"

mkdir -p "$(dirname "$STATE")"
cat > "$STATE" <<EOF
{
  "date": "$(date +%F)",
  "total": $TOTAL,
  "failed": $FAILED,
  "scripts": $SCRIPTS_JSON
}
EOF

echo "Selftests: $FAILED/$TOTAL FEHLGESCHLAGEN"
[ "$FAILED" -eq 0 ]
