#!/usr/bin/env python3
# Summary (English; the German docstring below is the long-form design record):
# PreToolUse hook for the Bash tool. In the MAIN checkout of the setup directory
# (~/.claude) it blocks git commands that move refs or rewrite files the live
# hooks read (merge, rebase, cherry-pick, reset, update-ref, branch switches, ...).
# Allowlist, not blacklist: read-only verbs, add/commit/fetch and a few sanctioned
# recovery forms (checkout/restore -- <path>, revert) pass; any other or unknown
# verb is denied. Worktrees and other repos are out of scope. Scope is decided
# from cwd, GIT_DIR/--git-dir and GIT_WORK_TREE/--work-tree. `git revert` is only
# allowed for targets in the history of a frozen anchor ref that the maintainer
# advances by hand. Mode-aware: "ask" in default/plan mode, "deny" in every
# auto-approve mode. Known gaps (e.g. `bash -c`) are listed in KNOWN_GAPS.
"""merge-gate.py — PreToolUse Bash-Gate gegen ref-bewegende git-Kommandos im
HAUPT-Checkout von ~/.claude.

Warum: Ein separates Schreib-Gate schuetzt scripts/*.py, agents/*.md, CLAUDE.md
und settings*.json gegen Write/Edit — auch in Worktrees. Der
Escape-Hatch dieses Schreib-Gates gibt Worktree-Pfade wieder frei, damit
autonome Mehrdatei-Arbeit in Worktrees ueberhaupt moeglich bleibt.
Ohne diesen Gate waere diese Exemption wertlos: ein
`git merge task/x` im Hauptcheckout traegt die Worktree-Fassung ungegated in die
live gelesenen Dateien. push-gate.py deckt das NICHT — es steigt bei jedem
Nicht-`push`-Verb aus, und sein eigener Selftest stellt fuer `git merge main`
ausdruecklich "kein Treffer" fest.

Anker-Richtung ALLOWLIST, nicht Blacklist:
eine selbstverfasste Blacklist ref-bewegender Verben ist mit Sicherheit
unvollstaendig (merge, rebase, cherry-pick, reset, pull, am, apply, stash pop,
read-tree, update-ref, symbolic-ref, replace, filter-branch, switch,
checkout <ref> -- <path>, restore --source, ...). Deshalb: eine kleine Menge
nachweislich harmloser Verben ist erlaubt, alles andere wird geblockt.
Unbekanntes Verb -> deny. Ein Vergessen geht damit konservativ aus.

Scope eng: feuert NUR, wenn das effektive Ziel exakt der Hauptcheckout ~/.claude
ist. NICHT in Worktrees (dort ist Arbeiten der Zweck, und der Worktree ist per
Konstruktion nicht das, was die Live-Hooks lesen), NICHT in fremden Repos (dort
ist der lokale Merge ausdruecklich sanktioniert).

Drei Scope-Quellen, per ODER verknuepft (cwd, git-dir, work-tree):

1. cwd (bzw. `-C`): das effektive Repo-Toplevel ist ~/.claude.

2. GIT_DIR / --git-dir. Der cwd allein reicht nicht:
   `GIT_DIR=~/.claude/.git git merge task/foo` aus beliebigem cwd bewegt Ref UND
   Index des Hauptcheckouts. Der Arbeitsbaum bleibt dabei zunaechst unveraendert
   — aber das anschliessende `git checkout -- .`, das die Blockmeldung des
   Schreib-Gates ausdruecklich empfiehlt und das dieser Gate bewusst erlaubt,
   materialisiert die fremde Fassung dann aus dem vergifteten Index. Beide Stufen
   sind fuer sich gate-konform; die Kette wird deshalb an Stufe 1 gebrochen.
   Empirisch verifiziert an einem Wegwerf-Repo.

3. GIT_WORK_TREE / --work-tree. Schlimmer als 2., weil einstufig:
   aus einem beliebigen fremden Repo schreibt `git --work-tree=~/.claude
   checkout -- <pfad>` den fremden Inhalt DIREKT in die Live-Dateien, ohne
   sanktionierten Zwischenschritt. An Wegwerf-Repos gemessen
   (git 2.55.0): `--work-tree=` und `GIT_WORK_TREE=` wirken beide; ein Ziel im
   UNTERverzeichnis von ~/.claude schreibt genauso hinein; und ein Ziel OBERHALB
   ($HOME) schreibt ueber den Index-Pfad `.claude/scripts/...` ebenfalls hinein.
   Deshalb prueft _hits_claude_work_tree auf Ueberlappung in BEIDE Richtungen,
   nicht auf Pfadgleichheit. `-c core.worktree=` ist dagegen wirkungslos
   (mit und ohne explizites --git-dir gemessen) und deshalb kein Vektor.
   Unter einem so umgelenkten Arbeitsbaum verlieren die sanktionierten Auswege
   ihre Grundlage — sie materialisieren dann nicht mehr aus dem Index des
   Hauptcheckouts, sondern aus einem fremden. checkout/restore/revert sind
   deshalb genau dann NICHT erlaubt (WORKTREE_WRITING_VERBS).
   Preis dieser Konservativitaet, offen benannt statt versteckt: auch ein
   Dotfiles-Workflow `git --git-dir=... --work-tree=$HOME checkout ...` laeuft in
   den Gate, ebenso das praktisch nie vorkommende
   `git -C ~/.claude --work-tree=. checkout -- <pfad>` (Arbeitsbaum explizit auf
   den eigenen Checkout umgelenkt — harmlos, wird trotzdem geblockt). Bewusst in
   Kauf genommen: die Gegenrichtung — pruefen, ob der Index "eigener" ist — waere
   im Irrtumsfall ein Loch statt nur Reibung.

4. IMPLIZITER Arbeitsbaum bei explizitem git-dir. Der Fall, den
   Punkt 2 und 3 zusammen NICHT abdeckten und der die checkout/restore/revert-
   Ausnahme komplett umging:

       cd ~/.claude
       git --git-dir=~/.claude/.git/worktrees/task-x checkout -- scripts/x.py

   Ein explizites git-dir OHNE --work-tree schaltet die Repo-Discovery ab und
   macht den cwd zum Toplevel des Arbeitsbaums. Punkt 3 sammelte den Arbeitsbaum
   aber nur aus EXPLIZITEM --work-tree/GIT_WORK_TREE, der implizite kam nie an —
   also fiel `checkout` in die Ausnahme und sprang vor die Scope-Pruefung.
   Ergebnis: die Task-Branch-Fassung landet aus dem Worktree-INDEX direkt in den
   scharfen Dateien. An Wegwerf-Repos gemessen (git 2.55.0), fuer
   `checkout`, `restore`, `revert`, per Flag und per GIT_DIR=, und ebenso mit
   einem voellig fremden Repo als Quelle. Gegenprobe: dasselbe Kommando mit
   `merge` war schon vorher geblockt — nur die ausgenommenen Verben liefen durch.

   Der Invariant, der die Ausnahme traegt, ist nicht "Ziel ist eigen", sondern:
   Quell-Index UND Ziel-Arbeitsbaum muessen BEIDE die eigenen sein. Deshalb:
   trifft der (explizite oder implizite) Arbeitsbaum ~/.claude, faellt die
   Ausnahme — es sei denn, die Quelle ist nachweislich der eigene Index, also
   ALLE git-dir-Angaben zeigen auf ~/.claude/.git selbst. Ein
   ~/.claude/.git/worktrees/<x> zaehlt NICHT als eigener Index; dessen Index
   spiegelt den Task-Branch, und genau das ist der Vektor.

   "ALLE", nicht "eine": git laesst das --git-dir-Flag gegen GIT_DIR= gewinnen
   und das letzte Flag gegen das erste (gemessen). Die Sammelliste
   hier modelliert diese Praezedenz nicht, deshalb muss die Ausnahme an der
   strengsten Lesart haengen — sonst oeffnet
   `GIT_DIR=~/.claude/.git git --git-dir=<worktree> checkout -- x` das Loch
   wieder (gemessen: das Flag gewinnt, gelesen wird der Worktree-Index).

   Die umgekehrte Richtung — eigener Index in einen FREMDEN Arbeitsbaum, etwa
   `GIT_DIR=~/.claude/.git git checkout -- .` aus /tmp — bleibt erlaubt: sie
   schreibt ~/.claude-Inhalt nach /tmp und kann die Live-Dateien nicht erreichen.

5. Fuehrende git-Optionen werden gegen eine ALLOWLIST bekannter Optionen ohne
   Argument gescannt. Eine unbekannte `-…`-Option einfach als ein Token zu
   ueberspringen waere ein vollstaendiger Allowlist-Bypass, weil git
   Optionen kennt, die ein SEPARATES Argument fressen und nicht in der
   Wert-Optionen-Liste stehen:

       git --shallow-file status merge --no-edit task/x

   Liest der Scanner `status` (den Optionswert) als Verb, ist das READ_ONLY ->
   durch, und git fuehrt den Merge aus. Im Wegwerf-Repo scharf nachgestellt:
   der Merge lief, `main` bewegte sich, die Datei bekam den Task-Branch-Inhalt.
   Mitgemessen (git 2.55.0, je `git <opt> ZZMARKER status`): separates Argument
   nehmen `-C`, `-c`, `--git-dir`, `--work-tree`, `--namespace`, `--attr-source`,
   `--config-env`, `--shallow-file` (`--super-prefix` existiert in 2.55 nicht
   mehr, steht praeventiv in der Liste). `--shallow-file` ist der scharfe Fall,
   weil sein Wert ein beliebiger Pfad sein darf — der Angreifer waehlt also
   frei ein READ_ONLY-Verb als Tarnung. Bei `--attr-source` muss der Wert ein
   gueltiger Tree-ish sein, bei `--config-env` ein `name=VAR`.
   Konsequenz und Anker: GIT_NOARG_OPTS listet die Optionen OHNE Argument
   vollstaendig; jedes andere fuehrende `-…`-Token macht das Verb unbestimmbar
   und wird als UNKNOWN_OPTION_VERB behandelt -> nie erlaubt. Eine kuenftige
   git-Option laeuft damit in Reibung, nicht in ein Loch.

6. FUEHRENDE SHELL-UMLENKUNGEN. Ein Praefix-Scan, der nur NAME=WERT und
   Wrapper-Woerter abtraegt und sonst abbricht, wird von einer fuehrenden
   Umlenkung ausgehebelt; sie ist keins von beidem:

       2>/dev/null git merge task/foo

   shlex liefert ["2>/dev/null", "git", "merge", "task/foo"]; bricht
   _strip_prefixes am ersten Token ab, ist basename("2>/dev/null") != "git" ->
   _parse_git gibt None -> nicht erkannt. Zehn Zeichen Praefix hebeln sonst
   JEDES geblockte Verb aus.
   Gemessen (bash 3.2 und zsh, je gegen ein Wegwerf-Repo): die
   Formen `>f`, `>>f`, `<f`, `2>f`, `2>>f`, `&>f`, `2>&1`, `>&2`, `3>f` laufen
   alle durch und fuehren das Kommando aus; `&>>f` nur in zsh (bash 3.2 lehnt es
   ab — zsh ist die macOS-Standardshell, also zaehlt es). Umlenkungen duerfen im
   Praefix beliebig mit Zuweisungen und Wrapper-Woertern mischen (gemessen:
   `2>/dev/null FOO=1 git …` und `FOO=1 2>/dev/null git …` laufen beide).

   Auch die selteneren Operatoren `<>`, `>|`, `<<<` (Here-String), `<<` und
   `>!` werden abgetragen: _REDIR_OP fuehrt den Operatorsatz explizit (siehe
   den Tokenizer-Abschnitt unten). `{fd}>f` steht als Restfall in KNOWN_GAPS.

   Zwei Formen, weil die zweite ein SEPARATES Zieltoken frisst: `2>/dev/null` ist
   vollstaendig, `2> /dev/null` nicht. Beim Wegstrippen der zweiten Form muss das
   Zieltoken mitgehen, sonst waere `2> /dev/null git merge` weiter blind. Dass
   dabei ein `git` verschluckt werden kann (`> git merge task/foo`), ist KEIN
   Loch, sondern deckungsgleich mit der Shell: dort legt es die Datei `git` an
   und sucht dann das Kommando `merge` (gemessen, git lief nicht).

   Dazu gehoert SPLIT_RE: Trennte es Segmente an jedem `&`, zerschnitte es
   `2>&1 git merge task/foo` in "2>" + "1 git merge task/foo" — das zweite
   Segment beginnt mit "1", nicht mit "git", der Aufruf bliebe also auch bei
   korrektem _strip_prefixes unsichtbar. Das `&` einer Umlenkung wird deshalb
   nicht als Trenner gelesen (`&` direkt nach `<`/`>` oder direkt vor `>`).
   Hintergrund-`&` und `&&` trennen unveraendert; `cmd & git merge` und
   `cmd &git merge` bleiben zwei Segmente (gemessen, beide fuehren git
   aus). `cmd&>f` ist dagegen in bash UND zsh die Umlenkung, kein Hintergrund
   (gemessen) — die Ausnahme bildet also die Shell ab, nicht eine Annahme.

Praefixe werden auf TOKEN-Ebene abgetragen, nicht per Regex auf dem rohen String. Grund: ein textuelles Wegstrippen von
`NAME=WERT ` waere der einfachste Weg, um an das eigentliche Verb zu kommen —
zerstoert aber genau die Information, die Punkt 2 und 3 brauchen. _strip_prefixes
frisst deshalb Wrapper-Woerter UND Zuweisungen in beliebiger Reihenfolge, gibt die
Zuweisungen aber zurueck; GIT_DIR/GIT_WORK_TREE werden daraus gelesen. shlex
uebernimmt dabei das Quoting, `GIT_DIR="wert mit leerzeichen"` bleibt intakt.

Case: der Binary-Name wird kleingeschrieben verglichen, Wrapper-Woerter ebenso —
APFS ist per Default case-insensitiv, `SUDO GIT merge` startet dort dieselben
Binaries wie `sudo git merge`. Namen von Umgebungsvariablen dagegen sind IMMER
case-sensitiv: `git_dir=` liest git nicht, wird also auch hier nicht gesammelt.

Pfadvergleiche laufen ueber os.path.samefile, nicht ueber realpath-Stringgleichheit:
APFS ist per Default case-insensitiv, `/Users/alice/.CLAUDE/.git` oeffnet also
dieselbe Datei, ergibt aber einen anderen realpath — ein reiner Stringvergleich
waere per Gross-/Kleinschreibung umgehbar (unter macOS/APFS
nachgemessen: realpath ungleich, samefile gleich). Das gilt fuer alle drei
Scope-Quellen, auch fuer die neue Arbeitsbaum-Pruefung.

Sanktionierte Auswege bleiben offen — die Form entscheidet, nicht das Verb:
`git checkout -- <path>`, `git restore -- <path>` und `git revert` sind
erlaubt, weil die Blockmeldung des Schreib-Gates sie ausdruecklich
empfiehlt. Beachte: das `--` ist bei checkout/restore PFLICHT (_restore_form_ok
verlangt es), `git restore <path>` OHNE `--` wird geblockt — Ausnahme: die
reine Index-Form `git restore --staged <path>` und `git reset [HEAD] --
<path>` sind ohne bzw. mit `--` erlaubt, weil sie nachweislich keine Datei
schreiben. Konservative Fehlerrichtung, aber bewusst dokumentiert statt
beschoenigt. `git checkout <ref> -- <path>` und
`git restore --source=<ref>` sind es nicht — sie ziehen fremden Inhalt herein.
`git checkout <branch>` / `git switch <branch>` ebenfalls nicht: ein
Branch-Wechsel im Hauptcheckout laesst die Live-Hooks ab sofort die
Task-Branch-Fassung lesen, ein vollwertiger Laundering-Pfad.

`git revert` ist an die eigene Historie gebunden (siehe unten): im
Hauptcheckout nur fuer Ziele aus der Historie eines Referenzstandes. Dieser
Referenzstand ist NICHT HEAD, sondern der eingefrorene Anker-Ref
`refs/worktree/harness-anchor` des Hauptcheckouts — HEAD ist nach einer
Ref-Verschiebung aus einem Worktree (siehe unten) selbst die vergiftete
Groesse. Ist der Anker nicht
gesetzt, blockt revert im Hauptcheckout vollstaendig (fail-closed).
Verb-seitig — ohne Repo-Kontext — bleibt revert unveraendert erlaubt; die
Bindung ist eine SCOPE-Pruefung wie die GIT_DIR/--work-tree-Pruefungen und
kostet nur dort Subprozesse.

Modus-aware wie die anderen Gates (im Auto-Approve ist weder permissions.ask
noch Hook-`ask` eine verlaessliche Sperre — nur `deny` haelt):
- default / plan                                     -> ask
- acceptEdits / bypassPermissions / unbekannter Mode -> deny

Fail-OPEN bei Subprozess-Fehler/Timeout/fehlendem cwd fuer die SCOPE-Bestimmung
(_is_claude_main_checkout) — bewusst: das separate Schreib-Gate ist der
eigentliche Schutz, und ein fail-closed wuerde bei jedem langsamen
`git rev-parse` JEDES git-Kommando in JEDEM Repo blocken.

GENAU UMGEKEHRT der Ancestor-Check des revert-Falls (_is_ancestor, gegen den
Anker): er ist fail-CLOSED. Nur ein explizites
returncode==0 erlaubt; fehlendes git-Binary, Timeout, Exception, rc=1 (kein
Ancestor), rc=128 (Ziel nicht aufloesbar) und ein NICHT GESETZTER Anker
fuehren alle zu DENY. Das ist hier tragbar, weil der Aufruf erst laeuft, WENN der
Scope schon feststeht und das Verb `revert` ist — er kann also nicht wie die
Scope-Pruefung jedes git-Kommando in jedem Repo treffen. Ein fehlendes
git-Binary (z.B. per PATH-Manipulation) und ein haengendes git (Sleep >
GIT_TIMEOUT) enden beide in DENY.
Die Scope-Bestimmung selbst bleibt fail-open — bei fehlendem git wird der
cwd-Scope gar nicht erst erkannt; der fail-closed Zweig greift dann ueber die
subprozessfreien Scopes (GIT_DIR/--work-tree), und REPO_CASES deckt genau diesen
Pfad ab.

Grenze: dieselbe Klasse wie push-gate.py. NICHT erkannt: git ueber Alias/Shell-
Funktion, `bash -c "..."`, Aufrufe aus Python-/Node-Skripten, Wrapper MIT eigenen
Flags (`env -i ... git ...`, `sudo -u x git ...` — der Praefix-Scan bricht am
ersten Flag ab), und alles ausserhalb des Bash-Tools (Terminal, IDE, andere
Session). Ein Layer, kein Zaun.

Ein Loch ist BEKANNT und bewusst offen (scharf gemessen, steht als
Fall in KNOWN_GAPS, damit es nicht als geschlossen gelesen wird):

Ref-Verschiebung ueber ein Worktree-git-dir:
   `git --git-dir=~/.claude/.git/worktrees/<x> update-ref refs/heads/main <sha>`
   bewegt den geteilten main-Ref. NICHT gefixt, weil identisch aus dem Worktree
   selbst mit normaler Discovery erreichbar (`git -C <wt> update-ref …`, gemessen)
   — und Worktrees laesst dieser Gate absichtlich frei. Nur die --git-dir-
   Schreibweise zu blocken waere Schein-Sicherheit. `git fetch <x> +task:main`
   erreicht dasselbe NICHT: git verweigert das Anfordern in einen ausgecheckten
   Branch (gemessen).

   ACHTUNG: Nach diesem ersten Schritt bewegen sich zwar die Live-DATEIEN noch
   nicht und der Index des Hauptcheckouts bleibt unberuehrt — der Kette fehlt
   aber kein Primitiv: die Index-only-Ausnahme (siehe unten) setzt den Index auf
   HEAD zurueck. Vollstaendig nachgestellt:

       1. update-ref bewegt main auf den Poison-Commit  (Ref-Verschiebung, Datei/Index noch sauber)
       2. git restore --staged <pfad>                   (Index-only-Ausnahme: Index := HEAD = Poison)
       3. git checkout -- <pfad>                        (sanktioniert: Datei := Index)
       -> die Live-Datei traegt den Poison-Inhalt.

   Bewusst NICHT per Ancestor-Check geschlossen (anders als bei revert), weil
   die Index-only-Formen kein Ziel nennen, das sich pruefen liesse: bei revert
   ist das ZIEL ein Argument, das sich gegen den eingefrorenen Anker pruefen
   laesst — hier IST HEAD die vergiftete Groesse, und ein Vergleich von HEAD
   gegen sich selbst beweist nichts.

   Die Sicherheits-OBERGRENZE bleibt dadurch unveraendert: Schritt 1 verlangt
   bereits beliebige git-Ausfuehrung in einem Worktree, und wer die hat, hat
   ohnehin den weiter oben dokumentierten `bash -c`-Gap. Die Kette gibt also
   keine neue Faehigkeit, sie verkuerzt nur den Weg — und steht hier, damit
   sie nicht uebersehen wird.

   Eine Verengung der Index-only-Ausnahme wurde geprueft und verworfen. Ziel
   einer Verengung waere, dass diese Kette nicht mehr aus lauter einzeln
   erlaubten Schritten besteht — ohne die beiden legitimen Index-only-Formen
   (`git restore --staged <pfad>`, `git reset HEAD -- <pfad>`) wieder zu
   blocken. Ergebnis nach Messung: keine der verfuegbaren Verengungen schliesst
   etwas, und eine haette einen der beiden legitimen Faelle wieder mitgenommen.
   Beide Befunde am Wegwerf-Repo (git 2.55.0) nachgestellt, nicht hergeleitet:

   a) `worktree_redirected` als Bedingung fuer die Index-only-Ausnahme ist bei
      dieser Kette ein No-Op. Schritt 2 laeuft als voellig gewoehnliches
      `git restore --staged <pfad>` bzw. `git reset HEAD -- <pfad>` mit
      cwd=Hauptcheckout: kein `--work-tree`, kein `--git-dir`, kein `GIT_*`.
      Gemessen ueber alle vier Aufrufformen (cwd und `-C ~/.claude`, je restore
      und reset): explicit_wt=False, implicit_wt=False, foreign_into_claude=False
      -> worktree_redirected ist IMMER False. Die Flagge war fuer diesen Schritt
      nie True; ein Veto daran traefe nur Faelle mit Umlenkung von aussen.
      Umgekehrt haette JEDE Verengung, die Schritt 2 faengt, auch die legitimen
      Index-only-Formen gefangen — sie sind byte-identisch dasselbe Kommando im
      selben Scope, mit derselben Repo-Signatur (HEAD != Index != Arbeitsbaum in
      beiden Faellen). PARSE_CASES halten die Messung fest: fuer genau diese
      Kommandos ist gitdirs == worktrees == [].

   b) Der schwerere Befund: die Kette braucht die Index-only-Ausnahme ueberhaupt
      nicht. Dieselbe Wirkung in ZWEI Schritten, ohne restore und ohne reset:

          1. Angreifer baut im Worktree P (Datei = POISON) und darauf C mit
             demselben Tree wie der aktuelle main-Stand ("cleanup").
          2. `git -C <worktree> update-ref refs/heads/main <C>`   (= Schritt 1 oben)
             -> Hauptcheckout: HEAD = C, Arbeitsbaum unveraendert, `git status` leer.
          3. `git revert --no-edit <C>` im Hauptcheckout
             -> die Live-Datei traegt POISON, committet auf main.

      Ein Ancestor-Check gegen HEAD DES HAUPTCHECKOUTS
      (`merge-base --is-ancestor <ziel> HEAD`) wuerde Schritt 3 erlauben, denn
      Schritt 2 hat genau dieses HEAD bewegt. Gemessen an einer Variante dieser
      Datei mit HEAD-Vergleich (CLAUDE_DIR auf das Wegwerf-Repo umgebogen):
      rc=0 und `pass`. Die Reflexivitaet auszuschliessen ("HEAD selbst nicht
      mehr erlauben") hilft nicht: mit einem leeren Kind-Commit D (gleicher
      Tree) als main-Ziel ist C ein STRIKTER Ancestor, rc=0 bleibt, und der
      revert schrieb POISON (ebenfalls gemessen).

      DIESEN SCHRITT 3 SCHLIESST DER EINGEFRORENE ANKER (Abschnitt unten): der
      Vergleich laeuft nicht gegen das bewegliche HEAD, sondern gegen den
      eingefrorenen Anker. Am selben Wegwerf-Aufbau nachgemessen: nach Schritt 2
      gibt `merge-base --is-ancestor <poison> HEAD` rc=0 (ein HEAD-Vergleich
      wuerde erlauben) und `merge-base --is-ancestor <poison>
      refs/worktree/harness-anchor` rc=1 (der Anker blockt). Das schliesst NUR
      diese eine Kette. Schritt 1 bleibt offen, und die Index-only-Variante
      (restore --staged + checkout --) fragt ueberhaupt keinen Anker — sie
      laeuft unveraendert durch.

   Konsequenz: Die Ref-Verschiebung ist kein Problem der Index-only-Ausnahme,
   sondern ein Schritt-1-Problem. Die Ausnahme macht die Kette nur in der Lage
   moeglich, in der die Trees nicht passen; wer Schritt 1 hat, hat den
   Laundering-Pfad ueber revert ohnehin. Die Ausnahme bleibt deshalb
   unveraendert — eine Verengung haette messbar keinen Angriff geschlossen und
   zwei legitime Formen geblockt.

   Der EINZIGE Ort, an dem die Kette mechanisch faellt, ist Schritt 1: ref-
   schreibende Kommandos AUS einem Worktree auf einen im Hauptcheckout
   ausgecheckten Branch. Bewusst nicht gebaut. Der Gate laesst Worktrees per
   Konstruktion frei; dort zu greifen hiesse, `refs/heads/main` von
   einem Feature-Branch unterscheiden zu muessen — ein normaler
   Worktree-Commit bewegt ebenfalls einen geteilten Ref im common dir — und damit
   genau die Blacklist ref-bewegender Verben zu bauen, an der die Anker-Richtung
   dieser Datei ausdruecklich nicht haengen soll. Das ist eine Policy-Entscheidung
   fuer den Maintainer, keine stille Code-Aenderung. Gemessen als Ist-Zustand: sowohl
   `git update-ref refs/heads/main <sha>` mit cwd=Worktree als auch
   `git -C <worktree> update-ref …` aus dem Hauptcheckout laufen durch; dasselbe
   Kommando direkt im Hauptcheckout blockt.

REVERT EINES FREMDEN COMMITS (`git revert <fremder-sha>`) — per Ancestor-Check
an eine Referenz gebunden:

   Der Angriff, im Wegwerf-Repo scharf
   nachgestellt: der Angreifer committet in einem Worktree erst die vergiftete
   Fassung, dann einen "cleanup"-Commit zurueck; `git revert <cleanup-sha>` im
   Hauptcheckout schreibt die vergiftete Fassung in die Live-Datei UND committet
   sie auf main. Das waescht fremden Commit-Inhalt genauso wie das gesperrte
   `git checkout <ref> -- <pfad>` und umgeht dabei auch ein Write/Edit-Gate, das
   nur Tool-Calls sieht, keine git-Subprozesse. `cherry-pick` —
   dieselbe Faehigkeit, andere Richtung — ist unconditional gesperrt;
   das ist die Referenz-Haerte, nicht die Referenz-Luecke.

   Wichtig fuer die Wahl der Reparatur: der Angriff braucht KEINE
   git-dir-/work-tree-Umlenkung. Ein Commit aus irgendeinem Worktree desselben
   Repos ist ueber den geteilten Object-Store auch vom Hauptcheckout aus per SHA
   erreichbar (nachgemessen: `git cat-file -t <cleanup-sha>` im
   Hauptcheckout findet den Commit, obwohl er nur auf dem Task-Branch haengt).
   Eine "nur bei Umlenkung blocken"-Loesung wie bei checkout/restore haette
   diesen Angriff also NICHT geschlossen.

   Deshalb: revert bleibt erlaubt, wenn JEDES Zielcommit Ancestor des
   Referenzstandes ist (`merge-base --is-ancestor` ist reflexiv, gemessen: rc=0
   fuer HEAD vs HEAD). Damit bleibt der von der Blockmeldung des Schreib-Gates
   beworbene Ausweg — das Zuruecknehmen eines EIGENEN
   Commits — offen, waehrend der Cleanup-Commit aus einem Worktree
   (rc=1, gemessen) und jedes nicht aufloesbare Ziel (rc=128) fallen.
   Die Pruefung haengt am SCOPE, nicht am Verb: ohne Repo-Kontext bleibt
   `git revert abc1234` verb-seitig erlaubt (siehe CASES), und in fremden
   Repos laeuft ueberhaupt kein Subprozess.

   REFERENZSTAND, praezisiert (gemessen): HEAD des Hauptcheckouts als Referenz
   haelt nur, SOLANGE refs/heads/main nicht selbst bewegt wurde. Nach einer
   Ref-Verschiebung aus einem Worktree (siehe oben) ist HEAD des Hauptcheckouts
   der Angreifer-Commit, und dann bestuende JEDES Ziel aus dessen Historie die
   Pruefung — reflexiv (HEAD selbst) wie auch strikt (Ancestor eines
   nachgeschobenen Kind-Commits), beides nachgestellt. Ein HEAD-Vergleich waere
   damit gegen den urspruenglichen Angriff geschlossen (der Cleanup-Commit auf
   einem Task-Branch, der NIE auf main gezogen wurde, faellt weiter mit rc=1),
   aber NICHT gegen einen Angreifer, der vorher die Ref-Verschiebung
   ausgefuehrt hat. Siehe den Abschnitt zur Verengung der Index-only-Ausnahme —
   dort steht auch, warum das kein Anlass ist, sie zu verengen. Deshalb ist der
   Bezugspunkt ein eingefrorener Ref (siehe unten) und nicht HEAD.

INDEX-ONLY-FORMEN von restore und reset sind erlaubt. Ohne diese Ausnahme
   wuerden zwei harmlose Formen geblockt:

       git -C ~/.claude restore --staged scripts/merge-gate.py   -> BLOCKED
       git -C ~/.claude reset HEAD -- scripts/merge-gate.py      -> BLOCKED

   Ursachen getrennt: `_restore_form_ok` verlangt ein literales `--`, das die
   Form `--staged <pfad>` nicht braucht; fuer `reset` gaebe es in
   `_verb_allowed` ueberhaupt keinen Zweig, JEDE reset-Form fiele damit auf das
   finale `return False` durch — auch die harmlose.

   Warum die Ausnahme in JEDEM Scope traegt, an git 2.55.0 gegen Wegwerf-Repos
   gemessen (nicht aus der Doku uebernommen, die Doku stuetzt sie nur):
   - `git restore --staged f.txt`: Arbeitsbaum-Datei blieb unveraendert (LOCAL),
     nur der Index-Eintrag ging auf HEAD zurueck. Gegenprobe `--staged --worktree`:
     Arbeitsbaum wurde auf v2 geschrieben.
   - `git reset HEAD -- f.txt` und `git reset -- f.txt`: HEAD unbewegt, Arbeitsbaum
     unveraendert, nur der Index geleert.
   - Der entscheidende Fall, weil er das worktree_redirected-Veto begruendet:
     mit `--git-dir=<fremd> --work-tree=<opfer>` liess `restore --staged f.txt`
     die Opfer-Datei auf OPFER-ORIGINAL stehen, waehrend dasselbe Kommando OHNE
     `--staged` sie mit FREMD-INHALT ueberschrieb. Ebenso blieb `reset HEAD -- f.txt`
     unter Umlenkung wirkungslos auf das Opfer. Deshalb greift die restore-Ausnahme
     VOR dem Veto, und deshalb steht `reset` NICHT in WORKTREE_WRITING_VERBS —
     kein Versehen, sondern die gemessene Eigenschaft.

   Die Quelle bleibt in beiden Faellen streng auf HEAD/Index festgenagelt.
   Nachgestellt, warum: `git reset other -- f.txt` legte FREMD in den INDEX (der
   Arbeitsbaum blieb LOCAL), und das anschliessende, ausdruecklich sanktionierte
   `git checkout -- f.txt` materialisierte FREMD in die Datei. Dieselbe
   Zwei-Schritt-Waesche wie beim bestehenden --source-Verbot. Deshalb: vor dem
   `--` darf nur `HEAD` oder nichts stehen, und --source ist auch in der
   --staged-Form in jeder Schreibweise verboten.

   Die Ausnahme fuer restore ist als ALLOWLIST gebaut, nicht als Ausschluss
   gefaehrlicher Flags — warum, steht im Abschnitt zu den Praefix-Abkuerzungen
   von Long-Options. Fuer reset gilt dasselbe Prinzip:
   vor dem `--` ist nur `HEAD` oder nichts zulaessig.

   Auf Verengung geprueft, weil die Ausnahme die Ref-Verschiebungs-Kette (siehe
   oben) aus lauter erlaubten Schritten macht: unveraendert gelassen, Begruendung
   und Messungen im Abschnitt zur Verengung der Index-only-Ausnahme. Kurz: die
   Kette laeuft auch ganz ohne die Ausnahme (per revert), und jede Verengung,
   die Schritt 2 faengt, faengt die beiden legitimen Index-only-Formen gleich mit.

   Konservative Restkanten, benannt statt versteckt: `git reset -q -- <pfad>` und
   `git reset --mixed HEAD -- <pfad>` sind faktisch harmlos (letzteres
   gemessen: nur Index, mit Deprecation-Warnung), fallen aber an der
   "kein Flag vor `--`"-Regel. `git reset --hard -- <pfad>` konnte in dieser
   Umgebung nicht gemessen werden — der Destructive-Command-Gate blockt die
   Schreibweise; die Form-Pruefung weist sie ohnehin unabhaengig von Gits eigener
   Argumentpruefung ab, genau darauf soll sie sich nicht verlassen.

SHELL-KONTROLLFLUSS-WOERTER stehen NICHT in WRAPPER_WORDS.
   `if`, `elif`, `while`, `until`, `then`, `do` als Wrapper zu behandeln erzeugt
   False Positives gegen harmlosen Code: ein mehrzeiliges `python3 -c "..."` mit
   der Zeile

       if 'git' in cmd.lower() or x:

   wird geblockt. Kette: SPLIT_RE zerlegt am Newline, `if` wird als Wrapper
   abgestreift, shlex entquotet `'git'` zum Kommandonamen `git`, `in` wird als
   Verb gelesen -> unbekanntes Verb -> Block mit `verb='in'`. Das trifft
   beliebigen harmlosen Code und Text und ist wiederkehrend, nicht exotisch.

   Fuer die Aufnahme als Wrapper gibt es keinen gemessenen Bypass — anders als
   bei sudo/env/time/nohup/command/xargs, die das Folgewort wirklich als Kommando
   ausfuehren und deshalb Wrapper bleiben.

   Die Woerter ersatzlos zu streichen waere dagegen ein Schutz-Wegfall:
   `if git merge task/foo; then …` (ebenso die `while`/`until`-Varianten) liefe
   durch (tokens[0] == "if" != "git" -> _parse_git gibt None). Beide Faelle
   lassen sich aber am ROHEN Folgewort unterscheiden. `_strip_control_flow`
   streift ein Kontrollfluss-Wort nur dann ab, wenn direkt danach ein NACKTES
   `git`/`gh` steht (oder ein Wrapper-Wort, damit `if ! git merge …` faellt).
   Entscheidend ist, dass diese Pruefung VOR shlex laeuft: `'git'` bleibt dort
   `'git'` und ist nicht `git`. Damit:

       if git merge task/foo; then …      -> geblockt
       while git merge task/foo; do …     -> geblockt
       if 'git' in cmd.lower() or x:      -> laeuft durch
       if git_status == "clean":          -> laeuft durch

   Die Woerter bleiben ausdruecklich AUS WRAPPER_WORDS heraus: sie fuehren das
   Folgewort nicht per se aus, die Bedingung gehoert an die Abstreif-Stelle.
   Restkante, benannt statt versteckt: eine PFAD- oder quote-qualifizierte Form
   nach dem Kontrollwort (`if /usr/bin/git merge …`, `if "git" merge …`) wird
   nicht abgestreift — Basename-Aufloesung an dieser Stelle wuerde harmlose
   Zeilen wie `if src/git in paths:` blocken. Steht als KNOWN_GAP,
   gleiche Klasse wie die offenen `bash -c "…"`, `alias`, `python -c`.

   `!` BLEIBT drin, bewusst abweichend von den uebrigen Woertern: `! git merge
   task/foo` fuehrt git aus (in zsh UND bash gemessen, git lief), es
   ist ein Ein-Token-Bypass derselben Klasse wie die fuehrenden
   Umlenkungen — und ein Segment, das mit einem alleinstehenden `!`-Token
   beginnt, kommt in harmlosem Text nicht vor. Die Kontrollfluss-Woerter tun das
   staendig; das ist der ganze Unterschied.

GECLUSTERTE KURZFLAGS duerfen das --source-Verbot nicht umgehen. Scharf
   nachgestellt:

       git restore -sother -- f.txt      -> Arbeitsbaum bekam FREMD
       git restore -qsother -- f.txt     -> Arbeitsbaum bekam FREMD
       git restore -qs other -- f.txt    -> Arbeitsbaum bekam FREMD

   Ein Test auf Token-Gleichheit (`token in ("-s", "--source")`) laesst alle
   drei durch: `-sother` ist damit nur "irgendein Flag" — `--` vorhanden, davor
   alles mit `-`, also erlaubt. Das
   ist inhaltlich derselbe Laundering-Pfad wie das ausdruecklich gesperrte
   `git checkout <ref> -- <pfad>`, nur in Kurzschreibweise. git parst geclusterte
   Kurzflags mit angehaengtem oder folgendem Wert (gemessen), der Gate muss es
   auch tun. _has_short_flag prueft deshalb den ganzen Cluster; ein evtl.
   angehaengter Wert wird mitgeprueft, was hoechstens ueber-erkennt (= blockt).

   Kleiner Over-Block, benannt statt stillschweigend: ein Token wie
   `-sfile` gilt als `-s`-Cluster. VOR dem `--` ist das korrekt (git liest es als
   Option). Dahinter waere es ein Dateiname — deshalb sehen alle Flag-Pruefungen
   ueber _flag_region nur den Bereich vor dem `--`, und ein Pathspec `-sfile`
   nach `--` blockt nicht. Bleibt: eine Datei namens `-sfile` OHNE `--`
   davor laeuft in den Block. Richtige Fehlerrichtung, Abhilfe ist das `--`.

PRAEFIX-ABKUERZUNGEN von Long-Options.
   git akzeptiert eindeutige Praefixe von Long-Options (gitcli(7)). Alles an
   git 2.55.0 im Wegwerf-Repo scharf nachgestellt, nicht aus der Doku uebernommen:

       git restore --staged --wor  -- f.txt   -> Arbeitsbaum GESCHRIEBEN (v2)
       git restore --staged --work -- f.txt   -> Arbeitsbaum GESCHRIEBEN (v2)
       git restore --sta --wor     -- f.txt   -> Arbeitsbaum GESCHRIEBEN (v2)
       git restore --sou=other     -- f.txt   -> Arbeitsbaum = FREMD
       git restore --so=other      -- f.txt   -> Arbeitsbaum = FREMD

   Zwei getrennte Konsequenzen:

   a) Die Index-only-Ausnahme darf nicht per Namensvergleich auf `--worktree`
      pruefen: `--wor` ginge sonst als "index-only" durch — VOR dem
      worktree_redirected-Veto. Damit waere
      `git --git-dir=<fremd> --work-tree=~/.claude restore --staged --wor --
      scripts/x.py` genau der Laundering-Pfad, den dieser Gate
      verhindern soll. Deshalb ist die Ausnahme eine ALLOWLIST
      (RESTORE_STAGED_ONLY_FLAGS) — vor dem
      `--` ist nur `--staged`/`-S` zulaessig, jedes andere Flag, ob abgekuerzt
      oder nicht, laesst die Ausnahme fallen. Das ist dieselbe Anker-Richtung
      wie GIT_NOARG_OPTS (Abschnitt 5) und wie der Verb-Kern der Datei; eine
      Blacklist muesste jede Abkuerzung jeder kuenftigen Option kennen.

   b) Auch das --source-Verbot darf kein Exact-Match sein: `--sou=task/foo` und
      `--so=task/foo` liefen sonst durch — im Hauptcheckout, ohne jede
      Umlenkung, und ziehen fremden Tree-ish in den Arbeitsbaum. Dieselbe
      Faehigkeit wie das gesperrte
      `git checkout <ref> -- <pfad>`. _looks_like_option prueft deshalb die
      Praefix-Relation statt Gleichheit, in der sicheren Richtung: was ein
      Praefix von `--source` sein KOENNTE, gilt als `--source`. Mehrdeutiges wie
      `--s` weist git selbst ab und blockt hier zusaetzlich.

   Warum keine "vollstaendige Optionsliste von git restore" als Anker: sie waere
   dieselbe Art Selbstauskunft, an der schon die Blacklist scheitert, und muesste
   bei jeder git-Version nachgezogen werden. Die Allowlist braucht sie nicht.

TOKENIZER: FUEHRENDE UMLENKUNG, WORT-INTERNES `#`, ZSH-PRECOMMAND-MODIFIER.
Drei unabhaengige Bypass-Ursachen, alle gegen die ECHTE Shell gemessen statt
gegen Dokumentation.

   a) FUEHRENDE UMLENKUNG. Ein Operatorsatz, der nur `>`/`>>`/`<` mit
      optionalem `&` kennt, laesst die Formen `<>`, `<<<`, `<<`, `>|`, `>>|`,
      `>!` durchfallen; ihr Token wuerde zum vermeintlichen Kommandonamen.
      Zusaetzlich darf SPLIT_RE das `|` von `>|`/`>>|` nicht mitten im
      Operator zerschneiden — sonst wurden aus `>|out git merge
      task/x` zwei Segmente, das zweite mit Kopf `out`.
      Der Operator-Satz ist deshalb explizit und laengste-Alternative-zuerst.

   b) `shlex.split(..., comments=True)` schneidet an JEDEM `#` ab, auch mitten
      im Wort. Keine Shell tut das — ein Kommentar beginnt nur am WORTANFANG.
      Folge: `rm -rf a#b <echtes-ziel>` verlore sein echtes Ziel, das Gate saehe
      nur noch `a`. Ein argv-Vergleich gegen zsh 5.9 bestaetigt das: ohne
      `comments=True` stimmt das argv ueberein, mit weicht es ab.

   c) WRAPPER_WORDS muss neben `sudo`/`xargs`/`time`/`env`/`nohup`/`command`
      auch die zsh-Precommand-Modifier `noglob`/`nocorrect`/`exec` und die
      Ausfuehrungs-Wrapper `timeout`/`nice`/`stdbuf`/`setsid` enthalten: sonst
      laeuft `noglob git merge task/x` mit Kopf `noglob` durch. Die vier
      Wrapper mit eigenen Optionen bekommen ueber WRAPPER_VALUE_FLAGS eine
      minimale Optionsbehandlung — sonst wuerde `timeout 5 git …` als Kopf `5`
      gelesen. `timeout` frisst nur eine DAUER (_DURATION_RE), nie ein Wort.

   WARUM _mask_quoted: die Lookarounds in SPLIT_RE (`(?<![<>])&(?!>)`,
   `(?<!>)|`) pruefen das ROHE Nachbarzeichen. Das ist KEINE
   Operator-Erkennung: in

        echo a\\>& git merge task/foo

   steht vor dem `&` zwar ein `>`, aber ein escaptes — zsh nimmt das `&` als
   Hintergrund-Operator und fuehrt `git merge` AUS (mit einem
   Sentinel-Skript gemessen). Ohne Maske verschmilzt beides zu einem Segment mit
   Kopf `echo` und der Aufruf bliebe unsichtbar. `_mask_quoted` ersetzt
   gequotete und escapte Zeichen laengentreu, bevor SPLIT_RE laeuft — die
   Lookarounds sehen danach ausschliesslich echte Operatoren. Bei
   unbalancierten Quotes wird das Original verwendet (mehr Splits = mehr
   geprueft = konservativ).

   ANKER STATT SELBSTAUSKUNFT: die Tokenizer-Regeln sind nicht nur gegen
   selbstgeschriebene Erwartungswerte geprueft, sondern gegen zwei
   Ground-Truth-Harnesses — (1) Kommandos in echtem zsh mit Sentinel-Skript,
   Frage "hat die Shell das Folgekommando wirklich ausgefuehrt?"; (2)
   argv-Vergleich wie oben. Diese Richtung ist Absicht: eine selbst verfasste
   Fallliste faengt nur die Bypaesse, die der Autor sich vorstellen konnte.
   Over-Blocks gegenueber der Shell bleiben moeglich; bekannte Ursachen:
   `||`-Kurzschluss, `command noglob`, `noglob FOO=1` — zsh fuehrt dort selbst
   nichts aus.

   NEBENEFFEKT, ausdruecklich benannt: eine Commit-Message mit eingebettetem
   Newline (siehe BEKANNTE FALSE POSITIVES unten) wird durch die Maskierung nicht
   mehr mitten durchgeschnitten.

`gh pr checkout` WIRD HIER MITERKANNT. Es schaltet den
Arbeitsbaum des cwd-Repos auf den PR-Branch um, also dieselbe Waesche, die
`git checkout <branch>` in diesem Checkout blockiert. Fuer den REMOTE-Zustand
ist das Kommando lesend, fuer den ARBEITSBAUM nicht. Die Erkennung liegt
bewusst in DIESER Datei, damit die Frage "wechselt dieses Kommando den Branch
in ~/.claude?" genau eine Antwortstelle hat.

   Scope: gh kennt weder `-C` noch `--git-dir`/`--work-tree`; der Arbeitsbaum ist
   immer der des cwd. Deshalb genau eine Scope-Frage (`_is_claude_main_checkout`).
   `-R/--repo` waehlt das REMOTE-Repo und verschiebt den Arbeitsbaum nicht — es
   rettet also nicht vor dem Block, und `-b` nimmt einen Wert, der nicht als
   Positional gelesen werden darf.

   ALIASE: `gh pr co` ist ein eingebauter Alias
   (`gh pr checkout --help`, Abschnitt ALIASES). `gh co` ist ein haeufiger
   NUTZER-Alias (`co: pr checkout` in `~/.config/gh/config.yml`). Gelesen wird
   deshalb diese Datei (ein Dateizugriff, kein Subprozess: `gh alias list` pro
   Bash-Call waere nicht bezahlbar), mit Rueckfall auf das Default `{"co"}`. Shell-Aliase (`!…`) werden
   uebersprungen und stehen als KNOWN_GAP — ihr Inhalt ist hier so wenig
   interpretierbar wie `bash -c`.

`cwd` AUS DEM HOOK-PAYLOAD WIRD AUF STRING GEPRUEFT. Ein ungeprueft
weitergereichtes `data.get("cwd") or ""` landet ueber evaluate() in
_resolve_cwd/_expand/_resolve und damit in os.path.expanduser/
os.path.join. Ein nicht-stringartiger cwd (z.B. eine Zahl) endet je nach
Kommandoform in einem uncaught TypeError — ein mit Exception gestorbener
PreToolUse-Hook liefert kein `deny`, Claude Code laesst das Tool dann laufen
(fail-open) — oder laeuft still am Scope vorbei: `os.path.isdir(123)` ist False,
`_scope` gibt None, das Kommando gilt als "nicht in ~/.claude". Behandlung
wie bei `tool_input.command`: nicht auswertbar ⇒ deny (`_cwd_of`,
UNPARSEABLE_CWD_REASON). Auch der falsy Fall (`0`, `False`) denyt, statt per
`or ""` stillschweigend zum leeren cwd zu werden.

   Der cwd stammt vom Harness, ein Agenten-Einflusspfad darauf ist nicht
   bekannt. Geprueft wird trotzdem, weil die Fehlerrichtung an dieser Stelle
   sonst offen waere und die Pruefung nur eine Zeile Grenzpruefung kostet.

DER REVERT-ANKER IST EIN EINGEFRORENER REF, NICHT HEAD.
Die eine Zeile, die alles andere in diesem Abschnitt begruendet: ein Vergleich
des revert-Ziels gegen HEAD DES HAUPTCHECKOUTS taugt nicht, denn genau dieses
HEAD ist die Groesse, die die Ref-Verschiebung aus einem Worktree
(`git -C <worktree> update-ref refs/heads/main <poison>`) bewegt. Ein Vergleich
gegen eine vom Angreifer kontrollierte Groesse beweist nichts. Deshalb:
`merge-base --is-ancestor <ziel> refs/worktree/harness-anchor`, gelesen im
Hauptcheckout.

   WAS DER ANKER SCHLIESST — GENAU EINE KETTE, NICHT DIE REF-VERSCHIEBUNG SELBST:
   Geschlossen ist ausschliesslich die STILLE REVERT-WAESCHE:
       1. `git -C <worktree> update-ref refs/heads/main <poison-descendant>`
       2. `git revert --no-edit <commit aus dessen Historie>` im Hauptcheckout
   Schritt 2 faellt, weil der Poison-Commit nicht in der Historie des
   eingefrorenen Ankers haengt (im Wegwerf-Repo beidseitig gemessen:
   gegen HEAD rc=0, gegen den Anker rc=1).

   NICHT GESCHLOSSEN, ausdruecklich und ohne Beschoenigung:
   - SCHRITT 1 SELBST bleibt offen. Dieser Gate sieht ref-schreibende Kommandos
     aus einem Worktree per Konstruktion nicht (siehe Ref-Verschiebung oben).
     Wer main verschieben will, kann es weiter.
   - DIE ZWEITE KETTE bleibt vollstaendig offen: nach Schritt 1 setzt
     `git restore --staged <pfad>` (Index-only-Ausnahme) den Index auf das
     vergiftete HEAD und das sanktionierte `git checkout -- <pfad>`
     materialisiert es. Auf diesem Weg wird NIE ein Anker konsultiert.
   - Wer den Anker als "die Ref-Verschiebung ist zu" liest, liest falsch. Sie
     bleibt offen.

   WARUM refs/worktree/… UND NICHT EIN GETEILTER REF (z.B. refs/harness/…):
   `refs/worktree/<name>` ist ein PRO-WORKTREE-Namespace. Im
   Wegwerf-Repo (git 2.55.0) gemessen: schreibt ein verlinkter Worktree
   `git update-ref refs/worktree/harness-anchor <poison>`, landet der Wert
   ausschliesslich in `.git/worktrees/<id>/refs/worktree/harness-anchor`; die
   Kopie des Hauptcheckouts unter `.git/refs/worktree/harness-anchor` bleibt
   unveraendert. Ein GETEILTER Ref waere dagegen exakt so schreibbar wie
   refs/heads/main selbst — also gar kein Anker. Gelesen wird immer mit
   cwd=CLAUDE_DIR, deshalb die Fassung des Hauptcheckouts.

   RESTLUECKE DIESER ISOLATION — GEMESSEN, NICHT ANGENOMMEN, UND SIE IST DER
   WICHTIGSTE SATZ IN DIESEM ABSCHNITT: git kennt den Praefix `main-worktree/`,
   mit dem ein verlinkter Worktree die PER-WORKTREE-Refs des HAUPTCHECKOUTS
   adressiert. `git -C <worktree> update-ref main-worktree/refs/worktree/
   harness-anchor <poison>` schreibt die Anker-Kopie des Hauptcheckouts direkt
   (rc=0, Datei danach mit fremdem SHA). In vier Aufrufformen
   nachgestellt: `-C <wt>`, cwd=<wt>, `--git-dir=<wt-admindir>` und
   `update-ref --stdin`. Auch die Erzeugung eines noch nicht existierenden
   Ankers funktioniert so. (Nur `update-ref -d` mit diesem Praefix weist git mit
   "bad name" ab — das Loeschen geht also nicht, das Ueberschreiben schon.)
   Konsequenz, ehrlich formuliert: der Anker ist aus einem Worktree NICHT
   unfaelschbar. Er zwingt einen Angreifer zu einem ZWEITEN, unueblichen
   Kommando mit einer Syntax, die in normalem Arbeiten nie vorkommt — das ist
   Reibung und ein gut greppbares Signal, keine Tuer, die zufaellt. Wer diese
   Restluecke wirklich schliessen will, muss den Anker AUS dem Ref-Store
   herausnehmen (Datei ausserhalb von ~/.claude/.git, per Gate geschuetzt) —
   bewusst NICHT Teil dieses Gates, weil es eine eigene Entscheidung ist.

   DER ANKER DARF NUR VON HAND VORRUECKEN. Harte Bedingung, kein Stilpunkt:
       git -C ~/.claude update-ref refs/worktree/harness-anchor (gepruefter-sha)
   getippt vom Maintainer im eigenen Terminal, nach Sichtpruefung des
   Standes. KEINE Automatik, insbesondere KEIN geplanter Job, der den Anker aus
   dem LOKALEN refs/heads/main nachzieht: nach Schritt 1 zoege genau das den
   Anker auf den vergifteten Commit und oeffnete still wieder, was der Anker
   schliesst. Eine kuenftige Automatik ist nur denkbar aus einer VERTRAUTEN
   EXTERNEN Quelle (z.B. einem extern geschuetzten Remote-Branch).
   Nebenbefund, praktisch nuetzlich: das Bootstrap-Kommando faellt im
   Hauptcheckout selbst in diesen Gate (`update-ref` ist kein erlaubtes Verb) —
   eine Claude-Code-Bash-Session kann den Anker im Hauptcheckout-Scope also
   nicht setzen. Aus einem Worktree per `main-worktree/`-Praefix schon (siehe
   Restluecke).

   PREIS, benannt statt versteckt: ein LEGITIMER eigener Commit, der neuer ist
   als der Anker, laesst sich nicht mehr reverten, bis der Maintainer den Anker
   nachzieht. Und solange gar kein Anker gesetzt ist, blockt `git revert` im
   Hauptcheckout vollstaendig (mit eigener, handlungsanweisender Meldung —
   reason `revert-kein-anker`). Das ist die konservative Fehlerrichtung dieser
   Datei, aber es ist eine echte Reibung im Alltag, kein Nulltarif. Die
   zielfreien Formen (`--abort`/`--continue`/`--quit`/`--skip`) sind bewusst
   ausgenommen: sie ziehen nichts herein und brauchen deshalb keinen Anker.

   ABHAENGIGKEIT: der Anker setzt voraus, dass rohe Dateisystem-Schreibzugriffe
   auf ~/.claude/.git per Bash geblockt sind (durch ein separates Schreib-Gate,
   das nicht Teil dieses Exports ist). Ohne diese Sperre ist
   `echo (sha) > ~/.claude/.git/refs/worktree/harness-anchor` erlaubt und der
   Anker damit trivial ueberschreibbar — dann ist er wirkungslos.

LESENDE branch/tag-LISTENFORMEN UND `merge-base` LAUFEN DURCH.

   Warum: Listenabfragen von `branch`/`tag` und `merge-base` sind lesend; sie zu
   blocken waere reine Reibung ohne Schutzwert.

   VIER REGELN:
     1. `merge-base` in READ_ONLY. Es rechnet nur, bewegt keinen Ref und
        schreibt keine Datei.
     2. LIST_VALUE_FLAGS: wertnehmende Listen-Flags fressen ihr Folgetoken,
        sonst liest `_list_form_ok` den Wert als Anlege-Positional.
     3. LIST_PATTERN_FLAGS: `--list`/`-l` nehmen KEINEN Wert, ihr `<pattern>`
        ist ein Positional — eigene Regel (`patterns_ok`).
     4. Dieselbe Behandlung fuer `tag`, nicht nur fuer `branch`.

   Zusaetzlich steht `--no-contains` in TAG_LIST_FLAGS wie in
   BRANCH_LIST_FLAGS: ein reines Filter-Flag aus `man git-tag`, das Listenmodus
   impliziert und keine mutierende Form oeffnet.

   WAS SICH NICHT OEFFNET: Positionals bleiben nur mit `--list`/`-l` erlaubt,
   ein Folgetoken mit `-` wird nicht geraten sondern geblockt, und alle
   mutierenden Formen (`branch -d/-D/-m/-M/-c/-u/--set-upstream-to/
   --edit-description`, `tag -d/-a/-f`, `remote add/remove/rename/set-url/
   prune/update`, `worktree add/remove/move/lock/prune/repair`) blocken
   unveraendert und sind einzeln in CASES festgeschrieben.

   BEWUSST STEHENGELASSENE REIBUNG: die Separat-Form eines Sort-Keys mit
   fuehrendem Minus, `git branch --sort -committerdate`, bleibt geblockt. Das
   Gate kann dort Wert und Flag nicht trennen und raet nicht (sonst liefe
   `git branch --format --list neuer-name` als "--format frisst --list" durch
   und legte an). Abhilfe im Alltag: `--sort=-committerdate`. Beide Formen
   liegen als Assertion in CASES, damit die Entscheidung nicht still kippt.

BEKANNTE FALSE POSITIVES (Over-Block, KEINE Sicherheitsluecke — die
Fehlerrichtung, die diese Datei ausdruecklich als tragbar einstuft). Getrennt von
KNOWN_GAPS gefuehrt, damit beides nicht vermischt wird:

- [DURCH DIE MASKIERUNG BEHOBEN, Eintrag bleibt als Record stehen]
  Commit-Message mit eingebettetem Newline:

        git commit -m "notes
        git merge is dangerous"

    Ohne Quote-Kontext wuerden SPLIT_RE/_segments auf ROHEM Text inklusive
    Newlines trennen und der Teil nach dem Newline als eigener git-Aufruf
    gelesen -> Block. `_mask_quoted` macht die Segmentierung quote-bewusst; der
    Fall liegt als Assertion in CASES. Rest-Kante: HEREDOC-Koerper sind
    weiterhin nicht quote-bewusst.

- Shell-Variablen in Pfad-Argumenten:
    `git --work-tree="$TMPDIR/opfer" …` wird geblockt. Zur Gate-Zeit
    ist der Wert unbekannt, `_expand` laesst `$TMPDIR` stehen, der Pfad gilt als
    relativ und wird gegen den cwd (~/.claude) aufgeloest -> scheinbarer Treffer.
    Konservativ richtig; Abhilfe im Alltag: absoluten Pfad ausschreiben.

`git remote -v` bleibt lesend: `git remote -v` und `git -C ~/.claude remote -v`
laufen durch — `_verb_allowed` laesst `remote -v` zu (`_sub(["-v"])` gibt None,
und None ist erlaubt). CASES deckt das verb-seitig ab, REPO_CASES mit Scope fuer
beide Aufrufformen.
"""
import json
import os
import re
import shlex
import subprocess
import sys

CLAUDE_DIR = os.path.realpath(os.path.expanduser("~/.claude"))
CLAUDE_GIT_DIR = os.path.join(CLAUDE_DIR, ".git")
# Das `&` einer Umlenkung ist kein Segmenttrenner: `2>&1` und `&>f` wuerden
# sonst mitten im Praefix zerschnitten. `&&` matcht als eigene Alternative
# vorher, Hintergrund-`&` (gefolgt von Leerzeichen oder Kommandowort) weiter.
# Dasselbe gilt fuer das `|` von `>|`/`>>|` (zsh-Clobber). Und beide
# Lookarounds sind NUR zusammen mit _mask_quoted korrekt — sie sehen sonst ein
# gequotetes/escaptes Zeichen fuer einen Operator an (gemessener Bypass, s. _mask_quoted).
SPLIT_RE = re.compile(r"\|\||&&|[;\n]|(?<!>)\||(?<![<>])&(?!>)")
ASSIGN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Platzhalter fuer maskierte (gequotete/escapte) Zeichen. Muss laengentreu
# ersetzt werden, damit die Match-Offsets auf dem ORIGINAL gueltig bleiben.
_MASK = "x"
# Vollstaendige Umlenkungs-OPERATOREN (bash+zsh), laengste Alternative zuerst.
# Eine knappere Form wie `(?:>>?|<)&?` kennt `<>`, `<<<`, `>|`, `>>|`, `>!`
# nicht — `<>fileX git merge …` und `>|out git merge …` liefen dadurch als
# "Kommando heisst <>fileX" bzw. gar zerschnitten durch. Alle Formen
# an zsh 5.9 gemessen: jede fuehrt das FOLGEwort als Kommando aus.
# `{fd}>f` steht bewusst NICHT drin: gemessen fuehrt WEDER zsh 5.9
# (`parse error`) NOCH das systemeigene bash 3.2.57 (`{fd}: command not found`)
# das Folgewort aus — die Form kann hier also nichts verstecken. Zusaetzlich
# frisst _strip_wrappers das fuehrende `{` ohnehin. Als KNOWN_GAP gefuehrt fuer
# den Fall, dass dieser Harness je auf bash >= 4.1 laeuft.
_REDIR_OP = (r"\d*"
             r"(?:&>>|&>|>>!|>>\||>>|>\||>!|>&|>|<<<|<<-|<<|<>|<&|<)")
# Operator OHNE Ziel (`2> /dev/null`): frisst zusaetzlich das Folgetoken.
REDIRECT_BARE_RE = re.compile(_REDIR_OP + r"\Z")
# Operator mit ANGEHAENGTEM Ziel (`2>/dev/null`): frisst nur sich selbst.
REDIRECT_ATTACHED_RE = re.compile(_REDIR_OP + r"\S+\Z")
# Echte Kommando-Wrapper (sie fuehren das Folgewort als Kommando aus) plus das
# Negations-`!`. Shell-KONTROLLFLUSS-Woerter stehen hier bewusst NICHT, siehe
# _strip_control_flow — sie erzeugen False Positives gegen harmlosen Text.
# Enthalten sind auch die zsh-Precommand-Modifier und die ueblichen
# Ausfuehrungs-Wrapper, jeder einzeln an zsh 5.9 verifiziert
# (`noglob git merge …` fuehrt merge aus).
WRAPPER_WORDS = {"sudo", "xargs", "time", "env", "nohup", "command",
                 "noglob", "nocorrect", "exec", "timeout", "nice", "stdbuf",
                 "setsid", "!"}
# Kontrollfluss-Woerter sind KEINE Wrapper (sie stehen bewusst nicht in
# WRAPPER_WORDS), werden aber wieder abgestreift — allerdings nur, wenn danach
# ein NACKTES `git`/`gh` bzw. ein Wrapper-Wort steht. Genau diese Bedingung
# trennt `if git merge task/foo; then` von `if 'git' in cmd.lower():`.
CONTROL_FLOW_WORDS = frozenset({"if", "elif", "while", "until", "then", "do"})
# Erstes Wort eines Segments, unquotet (ein gequotetes `'if'` matcht nicht).
_CONTROL_HEAD_RE = re.compile(r"\A([A-Za-z]+)[ \t]+")
# Rohes Folgewort, VOR shlex — deshalb ist `'git'` hier `'git'` und nicht `git`.
_BARE_WORD_RE = re.compile(r"\A([^\s;|&()]+)")
# Wrapper mit EIGENEN Optionen: sonst bliebe deren Wert als vermeintliches
# Kommando stehen (`timeout 5 git merge …` -> Kopf waere "5"). Nur diese vier
# bekommen Optionsbehandlung; `sudo -u x git …` bleibt offen.
WRAPPER_VALUE_FLAGS = {
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "stdbuf": {"-i", "--input", "-o", "--output", "-e", "--error"},
    "setsid": set(),
}
# Nur eine DAUER darf als timeout-Argument gefressen werden — ein Kommandoname
# passt hier nie hinein, Ueberfressen ist damit ausgeschlossen.
_DURATION_RE = re.compile(r"\A\d+(?:\.\d+)?[smhd]?\Z")
# Fuehrende git-Optionen, die ein SEPARATES Argument fressen. Vollstaendigkeit
# ist hier sicherheitsrelevant: fehlt eine, liest der Scanner ihren Wert als Verb.
# An git 2.55.0 einzeln nachgemessen.
GIT_VALUE_OPTS = ("-C", "-c", "--git-dir", "--work-tree", "--namespace",
                  "--attr-source", "--config-env", "--shallow-file",
                  "--super-prefix")
# Fuehrende git-Optionen OHNE Argument — die ALLOWLIST, an der das Verb-Scanning haengt. Alles
# andere mit fuehrendem `-` macht das Verb unbestimmbar (siehe _scan_options).
GIT_NOARG_OPTS = frozenset({
    "-v", "--version", "-h", "--help", "-p", "--paginate", "-P", "--no-pager",
    "--no-replace-objects", "--no-lazy-fetch", "--no-optional-locks",
    "--no-advice", "--bare", "--literal-pathspecs", "--glob-pathspecs",
    "--noglob-pathspecs", "--icase-pathspecs", "--html-path", "--man-path",
    "--info-path", "--exec-path",
})
# Platzhalter-Verb, wenn eine unbekannte fuehrende Option das echte Verb
# verdeckt. Steht in keiner Allowlist -> _verb_allowed sagt immer False.
UNKNOWN_OPTION_VERB = "<unbekannte-git-option>"
GIT_TIMEOUT = 2.0

# Der EINGEFRORENE Anker, gegen den `git revert` geprueft wird (nicht HEAD).
# `refs/worktree/…` ist ein PRO-WORKTREE-Namespace — gelesen wird immer
# die Fassung des HAUPTCHECKOUTS unter .git/refs/worktree/. Ein Worktree, der
# denselben Namen unqualifiziert schreibt, trifft nur seine eigene Kopie unter
# .git/worktrees/<id>/refs/worktree/ (gemessen). Zur GEMESSENEN
# Restluecke dieser Isolation (`main-worktree/`-Praefix) siehe Docstring.
ANCHOR_REF = "refs/worktree/harness-anchor"

# Bewegt keinen Ref, aendert keinen Arbeitsbaum-Inhalt aus fremden Commits.
# `add`/`commit` sind hier bewusst drin: um eine guarded Datei zu committen,
# muss man sie erst geschrieben haben — und das blockt ein separates Schreib-Gate.
READ_ONLY = {
    "status", "log", "diff", "show", "blame", "annotate", "rev-parse", "rev-list",
    "for-each-ref", "ls-files", "ls-tree", "ls-remote", "describe", "shortlog",
    "merge-base",
    "grep", "cat-file", "count-objects", "diff-tree", "whatchanged", "cherry",
    "range-diff", "verify-commit", "verify-tag", "check-ignore", "check-attr",
    "reflog", "fetch", "help", "version", "add", "commit", "revert", "gc",
    "fsck", "maintenance", "var", "hash-object",
}

# Schreiben den Arbeitsbaum aus dem Index/aus Commits. Nur relevant, wenn der
# Arbeitsbaum nach ~/.claude umgelenkt wurde — dann ist der Index nicht mehr
# zwingend der des Hauptcheckouts, und die Ausweg-Begruendung faellt weg.
WORKTREE_WRITING_VERBS = {"checkout", "restore", "revert"}

# `git revert`-Formen ohne Zielcommit: sie setzen einen laufenden Ablauf fort
# oder brechen ihn ab und ziehen nichts Neues herein (git 2.55: `git revert
# (--continue | --skip | --abort | --quit)`).
REVERT_NO_TARGET_FLAGS = {"--continue", "--abort", "--quit", "--skip"}
# `git revert`-Optionen mit SEPARATEM Argument — an git 2.55.0 einzeln
# nachgemessen (`git revert <opt> <wert> ZZMARKER` -> "bad revision ZZMARKER",
# der Wert wurde also gefressen). `-S`/`--gpg-sign` steht bewusst NICHT hier:
# gemessen frisst es das Folgetoken nicht (`git revert -S HEAD` revertet HEAD).
# Fehler in dieser Liste gehen konservativ aus: ein faelschlich als Ziel
# gelesener Optionswert ist kein Commit -> rc!=0 -> DENY.
REVERT_VALUE_FLAGS = {"-m", "--mainline", "--strategy", "--cleanup",
                      "-X", "--strategy-option"}


def _mask_quoted(command):
    """Ersetzt gequotete/escapte Zeichen laengentreu durch _MASK.

    Zweck: SPLIT_RE darf ausschliesslich ECHTE Shell-Operatoren sehen. Die
    Lookarounds pruefen sonst das ROHE Nachbarzeichen — und `echo a\\>& git merge
    task/x` sieht damit aus wie die Umlenkung `>&`, obwohl zsh dort das `&` als
    Hintergrund-Operator nimmt und git AUSFUEHRT (mit einem
    Sentinel-Skript gemessen: das Folgekommando lief). Am Zeichen statt am
    Operator zu pruefen ist genau der Fehler, den die Maskierung vermeidet.

    Bei unbalancierten Quotes wird das Original zurueckgegeben: dann splittet die
    alte, groebere Logik weiter (mehr Segmente = mehr geprueft = konservativ).
    """
    out = []
    quote = None
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if quote is None:
            if c == "\\" and i + 1 < n:
                out.append(_MASK)
                out.append(_MASK)
                i += 2
                continue
            if c in ("'", '"'):
                quote = c
                out.append(_MASK)
                i += 1
                continue
            out.append(c)
            i += 1
            continue
        if quote == '"' and c == "\\" and i + 1 < n:
            out.append(_MASK)
            out.append(_MASK)
            i += 2
            continue
        if c == quote:
            quote = None
        out.append(_MASK)
        i += 1
    if quote is not None:
        return command
    return "".join(out)


def _segments(command):
    # Getrennt wird auf der MASKE, geschnitten aber aus dem ORIGINAL — deshalb
    # muss _mask_quoted laengentreu sein.
    masked = _mask_quoted(command)
    parts = []
    last = 0
    for m in SPLIT_RE.finditer(masked):
        parts.append(command[last:m.start()])
        last = m.end()
    parts.append(command[last:])
    return [p.strip() for p in parts if p.strip()]


def _strip_control_flow(segment):
    """`if`/`while`/`until`/… nur vor einem NACKTEN git/gh abstreifen.

    Diese Woerter bedingungslos als Wrapper zu behandeln erzeugt einen False
    Positive: `if 'git' in cmd.lower():` wuerde zu
    `'git' in cmd.lower():`, shlex entquotete zum Kommandonamen `git`, `in` galt
    als Verb -> Block. Sie bedingungslos NICHT abzustreifen wuerde dagegen
    `if git merge task/foo; then` aufreissen.

    Geprueft wird deshalb das ROHE Folgewort, vor shlex: `git`/`gh` unquotet
    (oder ein Wrapper-Wort, damit `if ! git merge …` weiter faellt). Alles
    andere — `'git'`, `git_status`, `git.merge(x)`, `src/git` — laesst das
    Segment unangetastet; sein Kopf bleibt `if` und _parse_git gibt None.
    """
    m = _CONTROL_HEAD_RE.match(segment)
    if m is None or m.group(1).lower() not in CONTROL_FLOW_WORDS:
        return segment
    rest = segment[m.end():]
    head = _BARE_WORD_RE.match(rest.lstrip("({ \t"))
    if head is None:
        return segment
    word = head.group(1).lower()
    if word in ("git", "gh") or word in WRAPPER_WORDS or word in CONTROL_FLOW_WORDS:
        return rest
    return segment


def _strip_wrappers(segment):
    previous = None
    while previous != segment:
        previous = segment
        segment = segment.lstrip("({ \t").rstrip(")} \t")
        if segment.startswith("$("):
            segment = segment[2:]
        segment = _strip_control_flow(segment)
    return segment


def _tokens(segment):
    # KEIN comments=True. shlex schneidet damit an JEDEM `#` ab, auch mitten
    # im Wort — `git merge task/a#b` verlor alles ab dem `#`. Keine Shell tut das:
    # ein Kommentar beginnt nur am WORTANFANG. Gegen zsh 5.9 per argv-Vergleich
    # gemessen (ohne comments=True identisch, mit comments=True Abweichungen).
    try:
        return shlex.split(segment)
    except ValueError:
        return segment.split()


def _eat_wrapper_args(tokens, word):
    # Optionen des Wrappers selbst, damit deren Wert nicht als Kommando gilt.
    flags = WRAPPER_VALUE_FLAGS.get(word)
    if flags is None:
        return
    while tokens and tokens[0].startswith("-") and tokens[0] != "-":
        flag = tokens.pop(0)
        if flag in flags and tokens:
            tokens.pop(0)
    if word == "timeout" and tokens and _DURATION_RE.match(tokens[0]):
        tokens.pop(0)


def _strip_prefixes(tokens):
    # Frisst Wrapper-Woerter, NAME=VALUE-Zuweisungen und Shell-Umlenkungen in
    # beliebiger Reihenfolge (so parst die Shell den Kommando-Praefix auch,
    # gemessen), gibt die Zuweisungen aber ZURUECK — in ihnen steckt ggf.
    # GIT_DIR/GIT_WORK_TREE, und die entscheiden ueber den Scope.
    assignments = []
    while tokens:
        head = tokens[0]
        if ASSIGN_RE.match(head):
            assignments.append(tokens.pop(0))
            continue
        if REDIRECT_BARE_RE.match(head):
            # `2> /dev/null`: das Ziel steht im naechsten Token und gehoert zur
            # Umlenkung, nicht zum Kommando. Die BARE-Pruefung muss vor der
            # ATTACHED-Pruefung stehen: sonst liest letztere `>>` als Operator `>`
            # mit angehaengtem Ziel `>` und laesst das echte Ziel als Kommando
            # stehen.
            tokens.pop(0)
            if tokens:
                tokens.pop(0)
            continue
        if REDIRECT_ATTACHED_RE.match(head):
            tokens.pop(0)
            continue
        word = os.path.basename(head).lower()
        if word in WRAPPER_WORDS:
            tokens.pop(0)
            _eat_wrapper_args(tokens, word)
            continue
        break
    return assignments


def _env_values(assignments, name):
    return [a.split("=", 1)[1] for a in assignments if a.startswith(name + "=")]


def _scan_options(tokens):
    """Fuehrende git-Optionen: (cdirs, gitdirs, worktrees, index_des_verbs).

    index_des_verbs ist None, wenn eine unbekannte Option das Verb verdeckt.
    """
    cdirs, gitdirs, worktrees = [], [], []
    i = 1
    while i < len(tokens):
        token = tokens[i]
        if token in GIT_VALUE_OPTS:
            if i + 1 < len(tokens):
                if token == "-C":
                    cdirs.append(tokens[i + 1])
                elif token == "--git-dir":
                    gitdirs.append(tokens[i + 1])
                elif token == "--work-tree":
                    worktrees.append(tokens[i + 1])
            i += 2
            continue
        if token.startswith("-C") and len(token) > 2:
            cdirs.append(token[2:])
            i += 1
            continue
        if token.startswith("--git-dir="):
            gitdirs.append(token.split("=", 1)[1])
            i += 1
            continue
        if token.startswith("--work-tree="):
            worktrees.append(token.split("=", 1)[1])
            i += 1
            continue
        # Angehaengter Wert (`--namespace=x`, `--exec-path=/x`, `--config-env=a=B`):
        # frisst kein weiteres Token. Nur Langoptionen — `-cfoo=bar` weist git ab
        # (gemessen), faellt hier also in den Unbekannt-Zweig.
        if token.startswith("--") and "=" in token:
            i += 1
            continue
        if token in GIT_NOARG_OPTS:
            i += 1
            continue
        if token.startswith("-"):
            # Unbekannte Option: ob sie das naechste Token frisst, ist nicht
            # entscheidbar — also ist das Verb nicht bestimmbar. Fail-safe.
            return cdirs, gitdirs, worktrees, None
        break
    return cdirs, gitdirs, worktrees, i


def _parse_git(segment):
    """(cdirs, verb, args, gitdirs, worktrees) oder None wenn kein git-Aufruf."""
    tokens = _tokens(_strip_wrappers(segment))
    assignments = _strip_prefixes(tokens)
    if not tokens or os.path.basename(tokens[0]).lower() != "git":
        return None
    cdirs, gitdirs, worktrees, i = _scan_options(tokens)
    gitdirs = _env_values(assignments, "GIT_DIR") + gitdirs
    worktrees = _env_values(assignments, "GIT_WORK_TREE") + worktrees
    if i is None:
        return cdirs, UNKNOWN_OPTION_VERB, [], gitdirs, worktrees
    if i >= len(tokens):
        return cdirs, None, [], gitdirs, worktrees
    return cdirs, tokens[i], tokens[i + 1:], gitdirs, worktrees


def _sub(args):
    for token in args:
        if not token.startswith("-"):
            return token
    return None


# Die EINZIGEN Tokens, die vor dem `--` stehen duerfen, damit die Index-only-
# Ausnahme von restore greift. Bewusst winzig und exakt (keine Praefixe): jede
# Abkuerzung wie `--sta` faellt aus der Ausnahme und wird wie jede andere restore-Form behandelt.
RESTORE_STAGED_ONLY_FLAGS = frozenset({"--staged", "-S"})


def _has_short_flag(args, letter):
    # Geclusterte Kurzflags: `-qsother` ist fuer git `-q -s other`,
    # an git 2.55.0 gemessen — das Ziel bekam den fremden Inhalt. Ein
    # Test auf Token-GLEICHHEIT (`token == "-s"`) sieht das nicht. Geprueft wird
    # deshalb der ganze Cluster einschliesslich eines evtl. angehaengten Wertes:
    # ueber-erkennen kostet nur Reibung, unter-erkennen oeffnet ein Loch.
    for token in args:
        if token.startswith("-") and not token.startswith("--") and letter in token[1:]:
            return True
    return False


def _flag_region(args):
    # Hinter `--` steht laut git-Konvention ausschliesslich Pathspec, nie ein
    # Flag: `git restore -- --staged` meint die DATEI `--staged` (gemessen — unter
    # Umlenkung bekam sie den fremden Inhalt). Flag-Pruefungen
    # duerfen deshalb nur diesen Bereich sehen, sonst liest man Dateinamen als
    # Optionen (und umgekehrt eine Option als harmlosen Pfad).
    return args[:args.index("--")] if "--" in args else args


def _looks_like_option(token, full):
    # git akzeptiert EINDEUTIGE PRAEFIXE von Long-Options (gitcli(7);
    # an git 2.55.0 gemessen: `--wor` wirkt wie `--worktree`, `--sou=x` wie
    # `--source=x`, beide fuehrten die volle Wirkung aus). Ein Exact-Match ist
    # deshalb wertlos als Sicherheitspruefung. Geprueft wird die Praefix-Relation
    # in der SICHEREN Richtung: alles, was ein Praefix von `full` sein KOENNTE,
    # gilt als `full`. Mehrdeutige Praefixe (`--s`) weist git selbst ab; hier
    # blocken sie zusaetzlich, was nur Reibung kostet.
    name = token.split("=", 1)[0]
    return name.startswith("--") and len(name) > 2 and full.startswith(name)


def _restore_source_present(args):
    # --source in JEDER Schreibweise: `-s`, `-s <wert>`, `-s<wert>`, `-qs<wert>`,
    # `--source <wert>`, `--source=<wert>` UND jede Praefix-Abkuerzung davon
    # (`--sourc`, `--sou=x`, `--so=x`) — alle gemessen, alle ziehen
    # fremden Tree-ish in Arbeitsbaum bzw. Index.
    region = _flag_region(args)
    for token in region:
        if _looks_like_option(token, "--source"):
            return True
    return _has_short_flag(region, "s")


def _restore_staged_only_form_ok(args):
    # `git restore --staged <pfad>` OHNE --worktree schreibt laut Doku und Messung
    # NUR den Index, nie den Arbeitsbaum — sicher in JEDEM Scope, auch unter
    # Arbeitsbaum-Umlenkung, weil dabei keine einzige Datei geschrieben wird. Ein
    # `--` ist deshalb hier NICHT Pflicht.
    #
    # ALLOWLIST, nicht Blacklist — dieselbe Anker-Richtung wie der ganze
    # Gate. Ein Ausschluss von `--worktree`/`--source` per Namensvergleich
    # waere sofort umgehbar, weil git eindeutige Praefixe akzeptiert:
    # `restore --staged --wor -- <pfad>` wirkt wie `--staged --worktree` und
    # schreibt unter Umlenkung die Live-Datei (gemessen). Eine
    # Blacklist muesste jede Abkuerzung jeder heutigen UND kuenftigen Option
    # kennen. Hier steht deshalb: vor dem `--` ist NUR `--staged`/`-S` erlaubt,
    # jedes andere Flag laesst die Ausnahme fallen — zurueck auf das Veto und
    # _restore_form_ok. Eine neue git-Option laeuft damit in Reibung, nicht in
    # ein Loch.
    #
    # Nur der Bereich vor `--` zaehlt: dahinter ist `--staged` ein PFADNAME.
    # `git restore -- --staged` stellt die Datei namens `--staged` im Arbeitsbaum
    # wieder her (gemessen, unter Umlenkung mit fremdem Inhalt) — die
    # Ausnahme darf dafuer nicht greifen.
    staged = False
    for token in _flag_region(args):
        if not token.startswith("-"):
            continue                      # Pathspec
        if token not in RESTORE_STAGED_ONLY_FLAGS:
            return False
        staged = True
    return staged


def _reset_form_ok(args):
    # Nur die Pathspec-Form: `git reset [HEAD] -- <pfad>...`. Sie bewegt weder
    # HEAD noch Arbeitsbaum, sondern setzt nur Index-Eintraege auf HEAD zurueck
    # (gemessen). Vor dem `--` darf deshalb NICHTS stehen ausser optional dem
    # einzelnen Token `HEAD`: ein fremder Tree-ish (`git reset task/foo -- x`)
    # vergiftet den Index genau wie restore --source (nachgestellt,
    # inkl. Materialisierung per sanktioniertem checkout). Flags sind ebenfalls
    # ausgeschlossen — `--hard`/`--soft`/`--mixed` sollen an DIESER Form scheitern,
    # nicht erst an Gits eigener Argumentpruefung.
    if "--" not in args:
        return False
    return args[:args.index("--")] in ([], ["HEAD"])


def _restore_form_ok(args):
    # Erlaubt nur die Pathspec-Wiederherstellung aus Index/HEAD: es muss ein
    # `--` geben, davor darf kein Ref stehen, und kein --source/-s.
    if _restore_source_present(args):
        return False
    if "--" not in args:
        return False
    return all(t.startswith("-") for t in args[:args.index("--")])


def _list_form_ok(args, list_flags, *, value_flags=frozenset(),
                  pattern_flags=frozenset()):
    # Erlaubt nur Listenformen: jedes Flag muss in list_flags stehen.
    # Wertnehmende Listen-Flags fressen ihr Folgetoken, sonst laese die Schleife
    # den Wert als Positional und blockte die reine Leseform (`git branch -a
    # --contains <sha>`). Beginnt das Folgetoken mit `-`, frisst git es zwar
    # trotzdem (parse-options nimmt das naechste argv unbesehen), das Gate kann
    # Wert und Flag dort aber nicht trennen -> Block statt Raten: sonst deckte
    # `git branch --format --list <name>` einen Anlege-Positional zu.
    # Nackte Positionals sind NUR bei einem pattern_flag erlaubt: ohne --list/-l
    # ist ein Positional bei `branch`/`tag` der Anlege-Name, kein Muster.
    patterns_ok = any(t.startswith("-") and t.split("=", 1)[0] in pattern_flags
                      for t in args)
    i = 0
    while i < len(args):
        token = args[i]
        if not token.startswith("-"):
            if not patterns_ok:
                return False
            i += 1
            continue
        name = token.split("=", 1)[0]
        if name not in list_flags:
            return False
        if name in value_flags and "=" not in token and i + 1 < len(args):
            if args[i + 1].startswith("-"):
                return False
            i += 2
            continue
        i += 1
    return True


BRANCH_LIST_FLAGS = {"-a", "-r", "-v", "-vv", "-l", "--list", "--all", "--remotes",
                     "--verbose", "--format", "--sort", "--contains", "--no-contains",
                     "--merged", "--no-merged", "--points-at", "--show-current",
                     "--color", "--no-color", "--column"}
TAG_LIST_FLAGS = {"-l", "--list", "-n", "--sort", "--format", "--contains",
                  "--no-contains", "--points-at", "--merged", "--no-merged",
                  "--column"}
# Listen-Flags mit SEPARATEM Wert. Aus `man git-branch`/`man git-tag` (git
# 2.55.0) abgelesen: `--contains [<commit>]`, `--no-contains [<commit>]`,
# `--merged [<commit>]`, `--no-merged [<commit>]`, `--points-at <object>`,
# `--format <format>`, `--sort <key>`. Die Menge ist bewusst der Anker (nicht
# das Verb); wirksam wird sie nur fuer Flags, die das jeweilige list_flags-Set
# ohnehin fuehrt. ACHTUNG, die Menge ist NICHT homogen — es stimmt NICHT, dass
# alle sieben laut Doku Listenmodus implizieren. Listenmodus implizieren nur die
# FILTER-Flags:
# bei `branch` `--contains`/`--no-contains`/`--merged`/`--no-merged`/
# `--points-at`, bei `tag` sagt `man git-tag` (2.55.0) das nur fuer
# `--contains`/`--no-contains`/`--points-at`, fuer `--merged`/`--no-merged`
# nicht. `--format` und `--sort` implizieren bei BEIDEN Verben KEINEN
# Listenmodus. Sie stehen hier trotzdem gefahrlos, aber aus einem ANDEREN
# Grund: ihr Argument ist Pflicht, das Folgetoken ist also immer der Wert und
# nie ein Anlege-Positional. Wer die Positional-Regel (`patterns_ok` in
# `_list_form_ok`) unter Berufung auf einen angeblich impliziten Listenmodus
# lockert, oeffnet die Anlegeform: `git branch --sort=committerdate neu` legt
# an, `git tag --format=%(refname) v1.0` ebenfalls.
LIST_VALUE_FLAGS = {"--contains", "--no-contains", "--merged", "--no-merged",
                    "--points-at", "--format", "--sort"}
# `-l`/`--list` nehmen KEINEN Wert, ihr `<pattern>...` ist ein Positional
# (git 2.55.0: "-l, --list  List branches. With optional <pattern>..."). Seit
# git 2.20 ist `-l` bei `branch` die Listenform und nicht mehr
# `--create-reflog`; gemessen an git 2.55.0.
LIST_PATTERN_FLAGS = {"-l", "--list"}
# config braucht eine eigene Regel: `git config --get user.name` hat einen
# legitimen Positional (den Key), `git config user.name x` darf nicht durch.
# Also: mindestens ein Lese-Flag, kein Schreib-Flag — Positionals egal.
CONFIG_READ_FLAGS = {"--get", "--get-all", "--get-regexp", "--get-urlmatch",
                     "--list", "-l"}
CONFIG_WRITE_FLAGS = {"--unset", "--unset-all", "--add", "--replace-all",
                      "--rename-section", "--remove-section", "--edit", "-e"}


def _verb_allowed(verb, args, *, worktree_redirected=False):
    if verb is None:
        return True
    # VOR dem Veto: die Index-only-Form von restore schreibt in keinem Scope eine
    # Datei, auch nicht unter Umlenkung (gemessen). Alle anderen
    # restore-Formen laufen unveraendert in das Veto und in _restore_form_ok.
    if verb == "restore" and _restore_staged_only_form_ok(args):
        return True
    if worktree_redirected and verb in WORKTREE_WRITING_VERBS:
        return False
    if verb in READ_ONLY:
        return True
    if verb == "config":
        names = {t.split("=", 1)[0] for t in args if t.startswith("-")}
        return bool(names & CONFIG_READ_FLAGS) and not names & CONFIG_WRITE_FLAGS
    if verb == "branch":
        return _list_form_ok(args, BRANCH_LIST_FLAGS, value_flags=LIST_VALUE_FLAGS,
                             pattern_flags=LIST_PATTERN_FLAGS)
    if verb == "tag":
        return _list_form_ok(args, TAG_LIST_FLAGS, value_flags=LIST_VALUE_FLAGS,
                             pattern_flags=LIST_PATTERN_FLAGS)
    if verb == "remote":
        return _sub(args) in (None, "show", "get-url")
    if verb == "stash":
        return _sub(args) in ("list", "show")
    if verb == "worktree":
        return _sub(args) in ("list",)
    if verb == "submodule":
        return _sub(args) in ("status", "summary")
    if verb == "notes":
        return _sub(args) in ("list", "show")
    if verb == "bisect":
        return _sub(args) in ("log", "view")
    if verb in ("checkout", "restore"):
        return _restore_form_ok(args)
    if verb == "reset":
        # `reset` steht bewusst NICHT in WORKTREE_WRITING_VERBS: die einzige hier
        # erlaubte Form schreibt nie den Arbeitsbaum, braucht das Veto also nicht
        # (unter Umlenkung gemessen). Jede andere reset-Form faellt schon an
        # _reset_form_ok, unabhaengig vom Scope.
        return _reset_form_ok(args)
    return False


# --- gh-Familie ------------------------------------------------------------
# `gh pr checkout` schaltet den Arbeitsbaum des cwd-Repos auf den PR-Branch um —
# dieselbe Waesche, die `git checkout <branch>` hier blockiert. Fuer den
# REMOTE-Zustand ist das Kommando lesend, fuer den ARBEITSBAUM nicht. Die
# Erkennung liegt bewusst HIER, damit "wechselt das den
# Branch in ~/.claude?" an genau einer Stelle beantwortet wird.
GH_PR_CHECKOUT_VERB = "gh pr checkout"
GH_ALIAS_CONFIG = os.path.expanduser("~/.config/gh/config.yml")
# gh-Flags mit SEPARATEM Wert, die vor dem Subkommando stehen koennen. Aus
# `gh pr checkout --help` (gh 2.x) abgelesen, nicht geraten.
GH_VALUE_FLAGS = {"-R", "--repo", "-b", "--branch"}
# Fallback, falls config.yml fehlt: der haeufige Alias `co`.
_GH_DEFAULT_ALIASES = frozenset({"co"})


def _gh_checkout_aliases(*, config_path=None):
    """Alias-Namen, die auf `pr checkout` zeigen.

    Gelesen aus gh's eigener config.yml statt per `gh alias list` — ein
    Subprozess pro Bash-Call waere hier nicht bezahlbar. Fehlt die Datei, gilt
    der haeufige Alias `co` als Fallback.
    """
    names = set(_GH_DEFAULT_ALIASES)
    try:
        with open(config_path or GH_ALIAS_CONFIG, "r", encoding="utf-8",
                  errors="replace") as fh:
            lines = fh.read().splitlines()
    except OSError:
        return frozenset(names)
    inside = False
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if not line[:1].isspace():
            inside = line.strip() == "aliases:"
            continue
        if not inside or ":" not in line:
            continue
        name, _, expansion = line.strip().partition(":")
        expansion = expansion.strip().strip("'\"")
        # Shell-Aliase (`!…`) fuehren beliebigen Code aus — dieser Gate kann da
        # nicht hineinsehen, sie stehen als KNOWN_GAP.
        if expansion.startswith("!"):
            continue
        words = expansion.split()
        if words[:2] in (["pr", "checkout"], ["pr", "co"]):
            names.add(name.strip())
    return frozenset(names)


GH_CHECKOUT_ALIASES = _gh_checkout_aliases()


def _gh_positionals(args):
    out = []
    i = 0
    while i < len(args):
        token = args[i]
        if token in GH_VALUE_FLAGS:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        out.append(token)
        i += 1
    return out


def _parse_gh(segment, *, aliases=None):
    """GH_PR_CHECKOUT_VERB, wenn das Segment den Arbeitsbaum umschaltet; sonst None."""
    tokens = _tokens(_strip_wrappers(segment))
    _strip_prefixes(tokens)
    if not tokens or os.path.basename(tokens[0]).lower() != "gh":
        return None
    words = _gh_positionals(tokens[1:])
    if not words:
        return None
    first = words[0].lower()
    if first in (aliases if aliases is not None else GH_CHECKOUT_ALIASES):
        return GH_PR_CHECKOUT_VERB
    if first == "pr" and len(words) > 1 and words[1].lower() in ("checkout", "co"):
        return GH_PR_CHECKOUT_VERB
    return None


def _expand(raw):
    return os.path.expanduser(os.path.expandvars(raw.strip("'\"")))


def _resolve(raw, base):
    # Relative Pfade loest git gegen das -C-Ziel auf, nicht gegen den urspruenglichen
    # cwd (fuer --git-dir und --work-tree gemessen) — deshalb `base`.
    d = _expand(raw)
    if os.path.isabs(d):
        return d
    return os.path.join(base, d) if base else ""


def _resolve_cwd(cdirs, cwd):
    result = cwd
    for raw in cdirs:
        d = _expand(raw)
        result = d if os.path.isabs(d) else (os.path.join(result, d) if result else "")
    return result


def _same_path(a, b):
    # samefile statt realpath-Vergleich: faengt Case-Varianten auf case-
    # insensitivem APFS und Symlink-/Hardlink-Aliase. Nicht existent -> OSError.
    try:
        return os.path.samefile(a, b)
    except OSError:
        return False


def _contains(outer, inner):
    # "inner liegt in outer (oder ist outer)" — komponentenweise per _same_path
    # statt Stringvergleich, sonst waere die Pruefung per Case-Variante umgehbar.
    # Ein noch nicht existierender inner-Pfad laeuft bis zum ersten existierenden
    # Vorfahren hoch und wird trotzdem erkannt.
    current = os.path.realpath(inner)
    while True:
        if _same_path(current, outer):
            return True
        parent = os.path.dirname(current)
        if parent == current:
            return False
        current = parent


def _hits_claude_git_dir(gitdirs, base):
    # Pfadgleichheit, bewusst KEINE Ueberlappung: ~/.claude/.git/worktrees/<x> ist
    # das gitdir eines Worktrees und genau der Fall, den dieser Gate freilaesst.
    for raw in gitdirs:
        d = _resolve(raw, base)
        if d and _same_path(d, CLAUDE_GIT_DIR):
            return True
    return False


def _hits_claude_work_tree(worktrees, base):
    # Ueberlappung in beide Richtungen, siehe Docstring (Abschnitt 3): ein Ziel unterhalb von
    # ~/.claude schreibt hinein, und ein Ziel oberhalb ($HOME) schreibt ueber den
    # Index-Pfad `.claude/...` ebenfalls hinein. Beides gemessen.
    for raw in worktrees:
        d = _resolve(raw, base)
        if d and (_contains(CLAUDE_DIR, d) or _contains(d, CLAUDE_DIR)):
            return True
    return False


def _only_claude_git_dir(gitdirs, base):
    # Quelle == eigener Index? Nur wahr, wenn ALLE git-dir-Angaben auf
    # ~/.claude/.git SELBST zeigen (Pfadgleichheit wie _hits_claude_git_dir, ein
    # ~/.claude/.git/worktrees/<x> zaehlt also nicht). "Alle" statt "eine", weil
    # diese Liste die git-Praezedenz Flag > GIT_DIR und letztes Flag > erstes
    # nicht modelliert — siehe Docstring (Abschnitt 4).
    if not gitdirs:
        return False
    return all(_same_path(_resolve(raw, base), CLAUDE_GIT_DIR) for raw in gitdirs)


def _is_claude_main_checkout(cwd):
    if not cwd or not os.path.isdir(cwd):
        return False
    try:
        proc = subprocess.run(["git", "-C", cwd, "rev-parse", "--show-toplevel"],
                              capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except Exception:
        return False
    if proc.returncode != 0:
        return False
    top = proc.stdout.strip()
    return bool(top) and _same_path(top, CLAUDE_DIR)


def _revert_targets(args):
    """Commit-ish-Operanden von `git revert`; None = Form ohne Zielcommit."""
    targets = []
    i = 0
    while i < len(args):
        token = args[i]
        if token in REVERT_NO_TARGET_FLAGS:
            return None
        if token in REVERT_VALUE_FLAGS:
            i += 2
            continue
        if token.startswith("-"):
            i += 1
            continue
        targets.append(token)
        i += 1
    return targets


def _git_env():
    # GIT_*-Variablen aus der Hook-Umgebung koennten das Repo verschieben, gegen
    # das hier aufgeloest wird — der Anker muss der Hauptcheckout bleiben.
    return {k: v for k, v in os.environ.items()
            if k not in ("GIT_DIR", "GIT_WORK_TREE", "GIT_INDEX_FILE",
                         "GIT_COMMON_DIR", "GIT_OBJECT_DIRECTORY",
                         "GIT_NAMESPACE", "GIT_CEILING_DIRECTORIES")}


def _anchor_rev():
    """SHA des eingefrorenen Ankers im HAUPTCHECKOUT, "" wenn nicht vorhanden.

    Gelesen wird mit cwd=CLAUDE_DIR, deshalb loest `refs/worktree/…` die Kopie
    des HAUPTCHECKOUTS auf (gemessen). `--verify --quiet` gibt bei
    fehlendem Ref rc=1 und leeren stdout; `^{commit}` weist ein Tag- oder
    Baum-Objekt als Anker ab.
    """
    try:
        proc = subprocess.run(
            ["git", "-C", CLAUDE_DIR, "rev-parse", "--verify", "--quiet",
             ANCHOR_REF + "^{commit}"],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, env=_git_env())
    except Exception:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _is_ancestor(rev, base):
    """FAIL-CLOSED: nur ein explizites returncode==0 gibt True.

    rc=1 (kein Ancestor), rc=128 (Ziel nicht aufloesbar), Timeout, fehlendes
    git-Binary und jede Exception fuehren zu False -> DENY. Bewusst anders als
    _is_claude_main_checkout, siehe Docstring. Leerer `base` (kein Anker
    gesetzt) ebenso.
    """
    if not rev or rev.startswith("-") or not base:
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", CLAUDE_DIR, "merge-base", "--is-ancestor", rev, base],
            capture_output=True, text=True, timeout=GIT_TIMEOUT, env=_git_env())
    except Exception:
        return False
    return proc.returncode == 0


def _revert_is_anchored(args, anchor):
    targets = _revert_targets(args)
    if targets is None:
        return True                      # --continue/--abort/--quit/--skip
    if not targets:
        return False                     # kein bestimmbares Ziel -> fail-closed
    return all(_is_ancestor(t, anchor) for t in targets)


def _scope(explicit_wt, foreign_into_claude, gitdirs, target):
    # Reihenfolge = Spezifitaet; _is_claude_main_checkout steht bewusst zuletzt,
    # es ist der einzige Zweig mit Subprozess.
    if explicit_wt:
        return "work-tree"
    if foreign_into_claude:
        return "git-dir-cwd"
    if _hits_claude_git_dir(gitdirs, target):
        return "git-dir"
    if _is_claude_main_checkout(target):
        return "cwd"
    return None


def evaluate(command, cwd):
    for segment in _segments(command):
        if _parse_gh(segment) is not None:
            # gh kennt weder -C noch --git-dir/--work-tree: der Arbeitsbaum ist
            # immer der des cwd. Deshalb genau EINE Scope-Frage, dieselbe wie bei
            # `git checkout <branch>` ohne Umlenkung. `-R/--repo` waehlt das
            # REMOTE-Repo und verschiebt den Arbeitsbaum nicht.
            scope = _scope(False, False, [], _resolve_cwd([], cwd))
            if scope is None:
                continue
            return {"segment": segment, "verb": GH_PR_CHECKOUT_VERB,
                    "scope": scope, "reason": "gh-branch-switch"}
        parsed = _parse_git(segment)
        if parsed is None:
            continue
        cdirs, verb, args, gitdirs, worktrees = parsed
        target = _resolve_cwd(cdirs, cwd)
        explicit_wt = _hits_claude_work_tree(worktrees, target)
        # Explizites git-dir OHNE --work-tree: git schaltet die Discovery ab und
        # nimmt den cwd als Arbeitsbaum-Toplevel (gemessen). Der Ausweg bleibt
        # nur offen, wenn die Quelle dann auch der eigene Index ist.
        implicit_wt = (not worktrees and bool(gitdirs)
                       and _hits_claude_work_tree([target], target))
        foreign_into_claude = implicit_wt and not _only_claude_git_dir(gitdirs, target)
        if _verb_allowed(verb, args,
                         worktree_redirected=explicit_wt or foreign_into_claude):
            # `revert` ist verb-seitig erlaubt (sanktionierter Ausweg), aber nur
            # fuer Ziele aus der Historie des eingefrorenen ANKERS (nicht HEAD).
            # Die Pruefung haengt am Scope: sie
            # laeuft nur, wenn das Ziel der Hauptcheckout ist, und kostet auch
            # nur dann Subprozesse.
            if verb != "revert":
                continue
            scope = _scope(explicit_wt, foreign_into_claude, gitdirs, target)
            if scope is None:
                continue
            anchor = _anchor_rev()
            if _revert_is_anchored(args, anchor):
                continue
            # Reihenfolge: die zielfreien Formen (--abort/--continue/…) sind
            # oben schon durch, sie brauchen keinen Anker. Erst DANACH wird ein
            # fehlender Anker gemeldet — sonst blockte ein nicht gebootstrappter
            # Anker auch das Abbrechen eines laufenden revert.
            if not anchor:
                return {"segment": segment, "verb": verb, "scope": scope,
                        "reason": "revert-kein-anker"}
            return {"segment": segment, "verb": verb, "scope": scope,
                    "reason": "revert-fremd"}
        scope = _scope(explicit_wt, foreign_into_claude, gitdirs, target)
        if scope is None:
            continue
        return {"segment": segment, "verb": verb, "scope": scope, "reason": "verb"}
    return None


# (command, expect_blocked_verb_wise) — reine Verb-/Formpruefung, ohne Repo- und
# ohne Pfad-Kontext. Die work-tree-Verschaerfung (WORKTREE_WRITING_VERBS) haengt
# an einem aufgeloesten Pfad und ist deshalb erst in REPO_CASES sichtbar.
CASES = [
    # Laundering-Pfade: muessen alle als "nicht erlaubt" klassifiziert werden.
    ("git merge task/foo", True),
    ("git merge --no-ff task/foo", True),
    ("git merge --squash task/foo", True),
    ("git rebase task/foo", True),
    ("git cherry-pick abc1234", True),
    ("git reset --hard task/foo", True),
    ("git reset --soft HEAD~1", True),
    ("git pull", True),
    ("git pull origin main", True),
    ("git am /tmp/p.patch", True),
    ("git apply /tmp/p.patch", True),
    ("git stash pop", True),
    ("git stash apply", True),
    ("git checkout task/foo", True),
    ("git switch task/foo", True),
    ("git checkout task/foo -- scripts/push-gate.py", True),
    ("git restore --source=task/foo scripts/push-gate.py", True),
    ("git restore -s task/foo -- scripts/x.py", True),
    ("git read-tree task/foo", True),
    ("git update-ref refs/heads/main task/foo", True),
    ("git symbolic-ref HEAD refs/heads/task/foo", True),
    ("git replace main task/foo", True),
    ("git filter-branch --all", True),
    ("git worktree remove x", True),
    ("git branch -D main", True),
    ("git branch -f main task/foo", True),
    ("git tag -f v1", True),
    ("git config user.name x", True),
    ("git config --unset user.name", True),
    ("git config --add remote.origin.fetch +refs/*:refs/*", True),
    ("git config --global user.email x@y", True),
    ("git stash", True),
    ("git rm scripts/x.py", True),
    ("git mv a b", True),
    ("git clean -fd", True),
    ("git zzz-unbekanntes-verb", True),          # Allowlist-Kern: unbekannt -> block
    ("npm test && git merge task/foo", True),
    ("sudo git merge task/foo", True),
    ("(git rebase main)", True),
    ("/usr/bin/git merge task/foo", True),
    ("git --no-pager merge task/foo", True),
    ("git -c core.editor=true merge task/foo", True),
    # Case-Insensitivity (macOS/APFS).
    ("GIT merge task/foo", True),
    ("Git merge task/foo", True),
    ("GIT rebase task/foo", True),
    ("GIT checkout task/foo", True),
    ("/opt/homebrew/bin/GIT merge task/foo", True),
    ("SUDO GIT merge task/foo", True),
    ("/usr/bin/SUDO git merge task/foo", True),
    # VAR=-Praefix: der git-Aufruf muss trotzdem als solcher erkannt werden.
    ("GIT_PAGER=cat git merge task/foo", True),
    ("GIT_PAGER=cat GIT merge task/foo", True),
    ('GIT_EDITOR="code -w" GIT merge task/foo', True),
    ("GIT_EDITOR='code -w' Git rebase main", True),
    ("GIT_DIR=/x/.git git merge task/foo", True),
    ("FOO=1 GIT_DIR=/x/.git BAR=2 git cherry-pick abc1234", True),
    ("env GIT_DIR=/x/.git git rebase main", True),
    ("GIT_DIR=/x/.git env git rebase main", True),
    ("SUDO GIT_DIR=/x/.git GIT merge task/foo", True),
    ("git --git-dir=/x/.git merge task/foo", True),
    ("git --git-dir /x/.git merge task/foo", True),
    # work-tree-Umlenkung — verb-seitig identisch, Pfad entscheidet spaeter.
    ("git --work-tree=/x merge task/foo", True),
    ("git --work-tree /x merge task/foo", True),
    ("GIT_WORK_TREE=/x git merge task/foo", True),
    ("GIT_TERMINAL_PROMPT=0 GIT_WORK_TREE=/x SUDO GIT merge task/foo", True),
    # Optionen mit separatem Argument. Der Wert darf nicht als Verb gelesen
    # werden — `--shallow-file status merge` liefe sonst als "status" durch und
    # fuehrt den Merge real aus (im Wegwerf-Repo nachgestellt).
    ("git --shallow-file status merge --no-edit task/x", True),
    ("git --shallow-file /tmp/s merge task/foo", True),
    ("git --attr-source status merge task/foo", True),
    ("git --attr-source main cherry-pick abc1234", True),
    ("git --config-env a=B merge task/foo", True),
    ("git --namespace status merge task/foo", True),
    ("git --shallow-file=/tmp/s merge task/foo", True),
    ("git --attr-source=main merge task/foo", True),
    # Unbekannte fuehrende Option -> Verb nicht bestimmbar -> Block.
    ("git --frobnicate merge task/foo", True),
    ("git --frobnicate status", True),
    ("git --future-option WERT status", True),
    ("git -cfoo=bar status", True),                # git weist -c angehaengt ab
    # Wert frisst das letzte Token, es bleibt kein Verb: git gibt nur die Usage
    # aus und fuehrt nichts aus (gemessen) -> nichts zu blocken.
    ("git --shallow-file status", False),
    # Fuehrende Shell-Umlenkung. Bricht _strip_prefixes am ersten Token ab,
    # hebelt JEDE dieser Formen den ganzen Gate aus.
    ("2>/dev/null git merge task/foo", True),
    (">/tmp/x git merge task/foo", True),
    (">>/tmp/x git merge task/foo", True),
    ("2>>/tmp/x git merge task/foo", True),
    ("</dev/null git rebase main", True),
    ("&>/tmp/x git merge task/foo", True),
    ("&>>/tmp/x git merge task/foo", True),
    ("2>&1 git merge task/foo", True),
    (">&2 git cherry-pick abc1234", True),
    ("3>/tmp/z git reset --hard task/foo", True),
    # Operator und Ziel als getrennte Tokens: das Ziel gehoert zur Umlenkung.
    ("2> /dev/null git merge task/foo", True),
    (">> /tmp/x git merge task/foo", True),
    ("< /dev/null git rebase main", True),
    # Umlenkung gemischt mit Zuweisungen, Wrappern und Case-Varianten.
    ("2>/dev/null GIT_DIR=/x/.git git merge task/foo", True),
    ("GIT_DIR=/x/.git 2>/dev/null SUDO GIT merge task/foo", True),
    ("2>/dev/null GIT_TERMINAL_PROMPT=0 SUDO GIT checkout task/foo", True),
    ("FOO=1 2> /dev/null env GIT rebase main", True),
    ("2>/dev/null git --shallow-file status merge --no-edit task/x", True),
    ("npm test 2>/dev/null && git merge task/foo", True),
    # Gegenprobe zur SPLIT_RE-Aenderung: Hintergrund-`&` trennt weiter, sonst
    # verschwaende das zweite Kommando im ersten Segment (gemessen:
    # beide Schreibweisen fuehren git aus).
    ("npm test & git merge task/foo", True),
    ("npm test &git merge task/foo", True),
    ("git merge task/foo & echo x", True),
    ("echo done & git status", False),
    # Umlenkung darf nicht UEBERblocken: lesende Verben bleiben lesend.
    ("2>/dev/null git status", False),
    ("2> /dev/null git log --oneline", False),
    ("&>/tmp/x git status", False),
    ("2>&1 git status", False),
    ("2>/dev/null git revert abc1234", False),      # Verb-Ebene, ohne Repo-Kontext
    ("2>/dev/null git checkout -- scripts/x.py", False),
    # Die Shell verschluckt hier das Kommandowort selbst: `> git` legt die Datei
    # `git` an und sucht dann das Kommando `merge` — git laeuft NICHT (gemessen).
    ("> git merge task/foo", False),
    ("2> git merge task/foo", False),
    # Erlaubt: lesend.
    ("git status", False),
    ("git log --oneline -20", False),
    ("git diff main..HEAD", False),
    ("git show HEAD", False),
    ("git rev-parse HEAD", False),
    ("git for-each-ref refs/heads", False),
    ("git fetch origin", False),
    ("git add scripts/x.py", False),
    ("git commit -m 'x'", False),
    ("git branch", False),
    ("git branch -a", False),
    ("git branch --show-current", False),
    ("git tag -l", False),
    ("git remote -v", False),
    ("git config --get user.name", False),
    ("git config --global --get user.email", False),
    ("git config --get-regexp '^user'", False),
    ("git config --list", False),
    ("git stash list", False),
    ("git worktree list", False),
    ("git submodule status", False),
    ("git bisect log", False),
    ("git", False),
    ("FOO=bar git status", False),
    ("GIT_DIR=/x/.git git log --oneline", False),
    ("GIT status", False),
    ("GIT log --oneline -20", False),
    ("GIT_PAGER=cat GIT status", False),
    ("GIT_WORK_TREE=/x git status", False),
    # Gegenrichtung: bekannte Optionen ohne Argument duerfen nicht ueberblocken.
    ("git --no-pager log --oneline", False),
    ("git -p log", False),
    ("git -P status", False),
    ("git --no-optional-locks status", False),
    ("git --literal-pathspecs status", False),
    ("git --no-advice --no-lazy-fetch status", False),
    ("git --exec-path=/opt/git status", False),
    ("git --namespace ns status", False),
    ("git --namespace=ns status", False),
    ("git --attr-source main status", False),
    ("git --shallow-file /tmp/s status", False),
    ("git --config-env=core.pager=VAR status", False),
    ("git --version", False),
    # Erlaubt: die sanktionierten Auswege.
    ("git checkout -- scripts/push-gate.py", False),
    ("git checkout -- .", False),
    ("git restore -- scripts/x.py", False),
    ("git revert abc1234", False),
    ("git revert --no-edit HEAD", False),
    ("GIT checkout -- scripts/push-gate.py", False),
    # ---- Index-only-Formen von restore/reset --------------------------------
    # Erlaubt — schreiben nachweislich keine Datei (gemessen).
    ("git restore --staged scripts/merge-gate.py", False),
    ("git restore --staged -- scripts/merge-gate.py", False),
    ("git restore -S scripts/x.py", False),
    ("git restore --staged .", False),
    ("git reset HEAD -- scripts/merge-gate.py", False),
    ("git reset -- scripts/merge-gate.py", False),
    ("git reset HEAD -- .", False),
    # Weiterhin geblockt: --source in jeder Schreibweise vergiftet den Index.
    ("git restore --staged --source=task/foo -- scripts/x.py", True),
    ("git restore --staged -s task/foo -- scripts/x.py", True),
    ("git restore --staged --source task/foo scripts/x.py", True),
    # --worktree hebt die Index-only-Eigenschaft auf -> zurueck auf den strengen
    # Pfad. Ohne `--` blockt der schon dort; MIT `--` ist es die sanktionierte
    # Form aus dem eigenen Index und faellt erst unter Umlenkung ans Veto (REPO_CASES).
    ("git restore --staged --worktree scripts/x.py", True),
    ("git restore -SW scripts/x.py", True),
    ("git restore --staged --worktree -- scripts/x.py", False),
    ("git restore -SW -- scripts/x.py", False),
    # reset: Quelle muss leer oder exakt HEAD sein, `--` ist Pflicht.
    ("git reset task/foo -- scripts/x.py", True),
    ("git reset other -- scripts/x.py", True),
    ("git reset HEAD~1 -- scripts/x.py", True),
    ("git reset HEAD scripts/x.py", True),
    ("git reset --hard -- scripts/x.py", True),
    ("git reset --soft HEAD -- scripts/x.py", True),
    ("git reset --mixed HEAD -- scripts/x.py", True),
    ("git reset -q -- scripts/x.py", True),        # konservative Kante, s. Docstring
    ("git reset", True),
    # Hinter dem `--` ist `--staged` ein PFADNAME und die Ausnahme darf nicht
    # greifen (gemessen: die Datei wird im Arbeitsbaum geschrieben).
    # Verb-seitig weiter erlaubt (sanktionierte Form), aber der REPO_CASE unter
    # Umlenkung beweist, dass das Veto sie faengt.
    ("git restore -- --staged", False),
    ("git restore -- --worktree", False),
    # ---- Praefix-Abkuerzungen von Long-Options -------------------------------
    # --source in Kurzform: zieht fremden Tree-ish, muss verb-seitig fallen.
    # (Ein Exact-Match-Vergleich liesse diese Formen durch.)
    ("git restore --sourc task/foo -- scripts/x.py", True),
    ("git restore --sou=task/foo -- scripts/x.py", True),
    ("git restore --so=task/foo -- scripts/x.py", True),
    ("git restore --s=task/foo -- scripts/x.py", True),
    ("git restore --staged --sou=task/foo -- scripts/x.py", True),
    ("git restore --staged --sourc task/foo scripts/x.py", True),
    ("git checkout --sou=task/foo -- scripts/x.py", True),
    # --worktree in Kurzform hebt die Index-only-Eigenschaft auf. Verb-seitig ist
    # das wie `restore -- <pfad>` erlaubt; entscheidend ist, dass die Ausnahme
    # NICHT greift und das Veto unter Umlenkung wieder faengt (siehe REPO_CASES).
    ("git restore --staged --wor -- scripts/x.py", False),
    ("git restore --staged --work -- scripts/x.py", False),
    ("git restore --sta --wor -- scripts/x.py", False),
    # Abgekuerztes --staged ohne `--` faellt aus der Ausnahme -> kein `--` -> Block.
    ("git restore --sta scripts/x.py", True),
    ("git restore --stage scripts/x.py", True),
    # Fremdes Flag vor dem `--` laesst die Ausnahme fallen (ALLOWLIST).
    ("git restore --staged --frobnicate scripts/x.py", True),
    ("git restore --staged -q scripts/x.py", True),
    # ---- geclusterte Kurzflags duerfen das --source-Verbot nicht umgehen -----
    ("git restore -sother -- f.txt", True),
    ("git restore -qsother -- f.txt", True),
    ("git restore -qs other -- f.txt", True),
    ("git restore -Ss other -- f.txt", True),
    ("git checkout -qs other -- f.txt", True),
    # ---- Kontrollfluss nur vor NACKTEM git abstreifen ------------------------
    # Der False Positive (blockte mit verb='in').
    ("if 'git' in cmd.lower() or x:", False),
    ("if 'git' in cmd:", False),
    ("while 'git' in line:", False),
    ("until 'git' in line:", False),
    ("if git_status == 'clean':", False),
    ("while gitdir and x:", False),
    ("if git.merge(x):", False),
    ("if src/git in paths:", False),
    # Der Schutz bleibt scharf: ein nacktes git nach dem Kontrollwort blockt.
    ("if git merge task/foo", True),
    ("while git merge task/foo", True),
    ("until git rebase main", True),
    ("then git merge task/foo", True),
    ("do git merge task/foo", True),
    ("elif git merge task/foo", True),
    ("if git status", False),
    ("if ! git merge task/foo", True),
    ("if noglob git merge task/foo", True),
    ("while timeout 5 git merge task/foo", True),
    ("if gh pr checkout 12", False),               # Verb-Ebene kennt kein gh
    # `!` bleibt Wrapper: fuehrt git wirklich aus (zsh und bash gemessen).
    ("! git merge task/foo", True),
    ("! git status", False),
    # Kein git-Aufruf.
    ("echo 'git merge task/foo'", False),
    ("echo 'GIT merge task/foo'", False),
    ("grep -rn 'git merge' scripts/", False),
    ("gh pr merge 12", False),
    # ---- Umlenkungs-Operatoren jenseits von `>`, `>>`, `<` -------------------
    # Alle Praefixe gegen zsh 5.9 mit einem Sentinel-Skript
    # gemessen: jeder fuehrt das FOLGEwort als Kommando aus. Die drei mit `*`
    # sind die Formen, die ein knapper Operatorsatz verpasst.
    ("<> /tmp/f git merge task/foo", True),          # *
    (">| /tmp/f git merge task/foo", True),          # *
    ("<<< wort git merge task/foo", True),           # *
    ("<>fileX git merge task/foo", True),
    (">|out git merge task/foo", True),
    (">>|out git merge task/foo", True),
    ("<<<hi git merge task/foo", True),
    (">& out git merge task/foo", True),
    (">&2 git merge task/foo", True),
    ("&>out git merge task/foo", True),
    ("&>> out git merge task/foo", True),
    ("> out 2>&1 git merge task/foo", True),
    ("<fileY >out 2>&1 git merge task/foo", True),
    (">&- git merge task/foo", True),
    # ---- zsh-Precommand-Modifier und Ausfuehrungs-Wrapper --------------------
    ("noglob git merge task/foo", True),
    ("nocorrect git merge task/foo", True),
    ("exec git merge task/foo", True),
    ("setsid git merge task/foo", True),
    ("timeout 5 git merge task/foo", True),
    ("timeout 5s git merge task/foo", True),
    ("timeout -k 1 5 git merge task/foo", True),
    ("timeout --preserve-status 5 git merge task/foo", True),
    ("nice git merge task/foo", True),
    ("nice -n 5 git merge task/foo", True),
    ("stdbuf -o0 git merge task/foo", True),
    ("stdbuf -oL -eL git merge task/foo", True),
    ("2>/dev/null noglob git merge task/foo", True),
    ("noglob 2>&1 git merge task/foo", True),
    ("noglob GIT_PAGER=cat git merge task/foo", True),
    # timeout frisst NUR eine Dauer — ein Kommandoname darf nie verschwinden.
    ("timeout git merge task/foo", True),
    # ---- Maskierung — Operatorzeichen in Quotes/hinter Backslash -------------
    # `echo a\>& git merge …`: zsh nimmt das `&` als Hintergrund-Operator und
    # fuehrt git AUS (gemessen). Ohne Maske halten die Lookarounds `>&` fuer eine
    # Umlenkung und verschmelzen beides zu EINEM Segment mit Kopf `echo` — der
    # Bypass, den die Maskierung schliesst.
    ("echo a\\>& git merge task/foo", True),
    ("echo a\\<& git merge task/foo", True),
    ("echo \"a>\"& git merge task/foo", True),
    ("echo 'a>'& git merge task/foo", True),
    ("echo a\\|b; git merge task/foo", True),
    ("echo 'a;b' && git merge task/foo", True),
    # Echte Umlenkung direkt vor einem echten Trenner: beides muss stimmen.
    ("echo hi >|out; git merge task/foo", True),
    ("echo hi 2>&1| git merge task/foo", True),
    ("echo hi <<<zz; git merge task/foo", True),
    # Gegenprobe: `>&` IST hier eine Umlenkung, das Folgewort ist ein DATEINAME
    # und wird nicht ausgefuehrt (gemessen — die Datei wurde ueberschrieben).
    ("echo hi >& git merge task/foo", False),
    # ---- kein comments=True --------------------------------------------------
    # shlex schnitt an JEDEM `#` ab, auch mitten im Wort; keine Shell tut das.
    ("git merge task/a#b", True),
    ("git merge --no-ff task/x#1", True),
    ("echo '# git merge task/foo'", False),
    # ---- Leseformen von branch/tag und merge-base ----------------------------
    # `merge-base` rechnet nur, es bewegt keinen Ref und schreibt keine Datei.
    ("git merge-base main HEAD", False),
    ("git merge-base --is-ancestor abc1234 HEAD", False),
    ("git merge-base --fork-point origin/main", False),
    # Wertnehmende Listen-Flags: der Wert ist KEIN Anlege-Positional.
    ("git branch -a --contains HEAD", False),
    ("git branch --contains abc1234", False),
    ("git branch --no-contains HEAD", False),
    ("git branch --merged main", False),
    ("git branch --no-merged main", False),
    ("git branch --points-at HEAD", False),
    ("git branch --sort committerdate", False),
    ("git branch --sort=-committerdate", False),
    # Separat-Form mit Dash-Wert: bewusst geblockt, s. Docstring (Listenformen). git frisst
    # `-committerdate` als Wert, das Gate kann es nicht von einem Flag trennen.
    ("git branch --sort -committerdate", True),
    ("git tag --sort -creatordate", True),
    ("git branch --format '%(refname)'", False),
    ("git branch --format=%(refname:short)", False),
    ("git branch -r --contains HEAD --format=%(refname)", False),
    ("git tag --contains HEAD", False),
    ("git tag --no-contains HEAD", False),
    ("git tag --points-at HEAD", False),
    ("git tag --merged HEAD", False),
    ("git tag --sort=-creatordate", False),
    # Wert fehlt (Flag steht am Ende): Doku-Default HEAD, weiterhin lesend.
    ("git branch --contains", False),
    ("git tag --contains", False),
    # --list/-l nehmen keinen Wert, ihr Muster ist ein Positional.
    ("git branch --list 'task/*'", False),
    ("git branch -l 'task/*'", False),
    ("git branch -a --list 'task/*' 'wip/*'", False),
    ("git tag -l 'v*'", False),
    ("git tag --list 'v1.*'", False),
    # Gegenrichtung: ohne Muster-Flag ist ein Positional der Anlege-Name.
    ("git branch neue-branch", True),
    ("git branch neue-branch main", True),
    ("git tag v1.0", True),
    ("git tag v1.0 HEAD", True),
    # Ein Positional NEBEN dem gefressenen Wert bleibt geblockt (bewusst
    # strenger als git, das ihn als Muster laese) — Anlegeform faellt zuerst.
    ("git branch --contains HEAD neue-branch", True),
    ("git tag --contains HEAD v1.0", True),
    # Folgetoken mit `-` ist nicht als Wert von einem Flag unterscheidbar: git
    # frisst es, das Gate raet nicht. Ohne diesen Zweig liefe der letzte Fall
    # als "--format frisst --list" durch und legte `neue-branch` an.
    ("git branch --format --list neue-branch", True),
    ("git branch --contains -D main", True),
    # Mutierende Formen von branch/tag: keine oeffnet sich durch die Listen-Regeln.
    ("git branch -d task/foo", True),
    ("git branch -m alt neu", True),
    ("git branch -M alt neu", True),
    ("git branch -c alt neu", True),
    ("git branch -u origin/main", True),
    ("git branch --set-upstream-to=origin/main", True),
    ("git branch --set-upstream-to origin/main", True),
    ("git branch --unset-upstream", True),
    ("git branch --edit-description", True),
    ("git branch --list 'task/*' -d task/foo", True),
    ("git tag -d v1.0", True),
    ("git tag -a v1.0 -m 'x'", True),
    ("git tag -l -d v1.0", True),
    # remote/worktree: unveraendert, aber vorher nur teilweise abgedeckt.
    ("git remote add x /tmp/y", True),
    ("git remote remove origin", True),
    ("git remote rename origin upstream", True),
    ("git remote set-url origin /tmp/y", True),
    ("git remote prune origin", True),
    ("git remote update", True),
    ("git worktree add /tmp/w task/foo", True),
    ("git worktree move /tmp/w /tmp/w2", True),
    ("git worktree lock /tmp/w", True),
    ("git worktree unlock /tmp/w", True),
    ("git worktree prune", True),
    ("git worktree repair", True),
]

# (command, verb, gitdirs, worktrees) — beweist, dass der WERT der Zuweisung die
# Praefix-Behandlung ueberlebt. Genau hier kollidieren die Ansaetze "Praefix wegstrippen"
# und "Praefixwert auswerten"; ohne diese Faelle bliebe die Aufloesung ungetestet.
PARSE_CASES = [
    ('GIT_DIR="/a b/.git" git merge task/foo', "merge", ["/a b/.git"], []),
    ("GIT_DIR='/a b/.git' GIT merge task/foo", "merge", ["/a b/.git"], []),
    ('GIT_WORK_TREE="/a b" GIT merge task/foo', "merge", [], ["/a b"]),
    ("GIT_TERMINAL_PROMPT=0 GIT_DIR=/x/.git GIT merge task/foo", "merge", ["/x/.git"], []),
    ("SUDO GIT_DIR=/x/.git git merge task/foo", "merge", ["/x/.git"], []),
    ("GIT_DIR=/x/.git SUDO GIT merge task/foo", "merge", ["/x/.git"], []),
    ("env GIT_DIR=/x/.git nohup GIT rebase main", "rebase", ["/x/.git"], []),
    ("FOO=1 GIT_DIR=/a/.git BAR=2 GIT_WORK_TREE=/b git cherry-pick abc", "cherry-pick",
     ["/a/.git"], ["/b"]),
    ("git --work-tree=/w --git-dir=/g/.git merge x", "merge", ["/g/.git"], ["/w"]),
    ("git --work-tree /w --git-dir /g/.git merge x", "merge", ["/g/.git"], ["/w"]),
    ("GIT_DIR=/e/.git git --git-dir=/o/.git merge x", "merge", ["/e/.git", "/o/.git"], []),
    # Env-Variablennamen sind case-sensitiv: `git_dir=` liest git nicht.
    ("git_dir=/x/.git git merge task/foo", "merge", [], []),
    ("GIT_PAGER=cat GIT status", "status", [], []),
    # Separat-argumentige Optionen duerfen das Verb nicht verschieben.
    ("git --shallow-file /tmp/s merge x", "merge", [], []),
    ("git --attr-source main --git-dir=/g/.git merge x", "merge", ["/g/.git"], []),
    ("git --config-env a=B --work-tree=/w merge x", "merge", [], ["/w"]),
    ("git --namespace ns --git-dir /g/.git merge x", "merge", ["/g/.git"], []),
    ("git --frobnicate merge x", UNKNOWN_OPTION_VERB, [], []),
    # Unbekannte Option nach einer Wert-Option: git-dir ist trotzdem schon erfasst.
    ("git --git-dir=/g/.git --frobnicate merge x", UNKNOWN_OPTION_VERB, ["/g/.git"], []),
    ("GIT_DIR=/e/.git git --frobnicate merge x", UNKNOWN_OPTION_VERB, ["/e/.git"], []),
    # Die Umlenkung faellt weg, die Zuweisungswerte ueberleben sie.
    ("2>/dev/null GIT_DIR=/x/.git git merge task/foo", "merge", ["/x/.git"], []),
    ("GIT_DIR=/x/.git 2>/dev/null SUDO GIT merge task/foo", "merge", ["/x/.git"], []),
    ("2> /dev/null GIT_WORK_TREE=/w git rebase main", "rebase", [], ["/w"]),
    ("&>/tmp/x git --git-dir=/g/.git merge x", "merge", ["/g/.git"], []),
    ("2>&1 git --work-tree=/w merge x", "merge", [], ["/w"]),
    (">> /tmp/x GIT_DIR=/a/.git env GIT cherry-pick abc", "cherry-pick", ["/a/.git"], []),
    # Schritt 2 der Ref-Verschiebungs-Kette traegt KEINEN Umlenkungs-Marker —
    # weder gitdirs noch worktrees. Genau deshalb ist `worktree_redirected` dort
    # strukturell False und eine Verengung der Index-only-Ausnahme daran
    # wirkungslos (gemessen). Diese Faelle halten die Messung fest.
    ("git restore --staged scripts/merge-gate.py", "restore", [], []),
    ("git reset HEAD -- scripts/merge-gate.py", "reset", [], []),
    ("git -C %s restore --staged scripts/merge-gate.py" % CLAUDE_DIR, "restore", [], []),
]

# Case-Varianten von ~/.claude. Auf case-insensitivem FS (APFS default) echte
# Vektoren, auf case-sensitivem FS existieren die Pfade nicht — die Erwartung wird
# deshalb aus dem FS abgeleitet statt hart auf True gesetzt.
CLAUDE_DIR_CASEVAR = os.path.join(os.path.dirname(CLAUDE_DIR),
                                  os.path.basename(CLAUDE_DIR).upper())
CLAUDE_GIT_DIR_CASEVAR = os.path.join(CLAUDE_DIR_CASEVAR, ".git")
HOME_DIR = os.path.dirname(CLAUDE_DIR)
CLAUDE_WT_DIR = os.path.join(CLAUDE_GIT_DIR, "worktrees")


def _some_worktree_git_dir():
    # Echter Worktree-gitdir, wenn es einen gibt — dann vergleicht _same_path zwei
    # existierende Verzeichnisse statt in den OSError-Zweig zu laufen. Sonst ein
    # fester Platzhalter, damit die Fallzahl nicht von der Umgebung abhaengt.
    try:
        for name in sorted(os.listdir(CLAUDE_WT_DIR)):
            path = os.path.join(CLAUDE_WT_DIR, name)
            if os.path.isdir(path):
                return path
    except OSError:
        pass
    return os.path.join(CLAUDE_WT_DIR, "task-x")


WT_GIT_DIR = _some_worktree_git_dir()


def _git_out(args):
    try:
        proc = subprocess.run(["git", "-C", CLAUDE_DIR] + args,
                              capture_output=True, text=True, timeout=GIT_TIMEOUT)
    except Exception:
        return ""
    return proc.stdout.strip() if proc.returncode == 0 else ""


def _main_history_sha():
    # Ein Commit, der wirklich in der Historie des Hauptcheckouts haengt.
    # Fallback HEAD ist ebenfalls Ancestor von HEAD -> Erwartung bleibt stabil.
    return _git_out(["rev-parse", "HEAD~1"]) or _git_out(["rev-parse", "HEAD"]) or "HEAD"


def _non_ancestor_sha():
    # Der revert-Waesche-Fall mit ECHTEM Objekt: ein Commit, der im geteilten
    # Object-Store liegt und per SHA aus dem Hauptcheckout erreichbar ist, aber
    # nicht in dessen Historie haengt (rc=1) — genau die Lage eines
    # Cleanup-Commits auf einem Task-Branch. Genommen wird der Tip eines nicht
    # gemergten Branches; gibt es keinen, faellt es auf den HEAD eines Worktrees
    # und zuletzt auf einen nicht aufloesbaren Platzhalter zurueck (rc=128).
    # Alle drei Varianten muessen DENY ergeben — Erwartung umgebungsstabil.
    # Geprueft wird gegen den ANKER, nicht gegen HEAD — sonst koennte
    # bei einem Anker HINTER HEAD ein "non-ancestor" gewaehlt werden, der gegen
    # den Anker trotzdem besteht.
    for source in (["for-each-ref", "--format=%(objectname)", "--no-merged", "HEAD",
                    "refs/heads"],
                   ["worktree", "list", "--porcelain"]):
        for line in _git_out(source).splitlines():
            sha = line.split(None, 1)[1] if line.startswith("HEAD ") else line.strip()
            if (re.fullmatch(r"[0-9a-f]{7,40}", sha)
                    and not _is_ancestor(sha, ANCHOR_SHA or "HEAD")):
                return sha
    return "abc1234abc1234abc1234abc1234abc1234abc1"


def _anchor_history_sha():
    # Ein Commit aus der Historie des ANKERS — der Fall, der weiter erlaubt sein
    # muss. Ohne Anker ist der Wert unbenutzt (dann blockt jeder revert).
    return (_git_out(["rev-parse", ANCHOR_REF + "~1"]) or ANCHOR_SHA
            or "HEAD")


def _post_anchor_sha():
    # Der KERNFALL des Ankers, aus der Umgebung abgeleitet: ein Commit, der
    # Ancestor von HEAD ist, aber NICHT des Ankers. Genau diese Lage stellt
    # die Ref-Verschiebung her (`update-ref refs/heads/main <poison>` aus einem
    # Worktree) — gegen HEAD bestuende so ein Ziel die Pruefung, gegen den
    # Anker nicht.
    # Steht der Anker auf HEAD, gibt es keinen solchen Commit; dann faellt es
    # auf NON_ANCESTOR_SHA zurueck, das ebenfalls DENY ergibt. Erwartung damit
    # umgebungsstabil True.
    if ANCHOR_SHA:
        for line in _git_out(["rev-list", "%s..HEAD" % ANCHOR_SHA]).splitlines():
            sha = line.strip()
            if re.fullmatch(r"[0-9a-f]{7,40}", sha):
                return sha
    return NON_ANCESTOR_SHA


def _revert_expect(rev):
    """Erwartung (blockiert?) fuer `git revert <rev>` im Hauptcheckout.

    Aus der UMGEBUNG abgeleitet statt hart gesetzt — wie die
    CLAUDE_DIR_CASEVAR-Faelle: ohne gesetzten Anker blockt der Gate JEDEN revert im
    Hauptcheckout (fail-closed), mit Anker nur die Ziele ausserhalb dessen
    Historie. Ein Selftest, der beides fest verdrahtet, waere entweder vor oder
    nach dem Bootstrap des Ankers rot.
    """
    if not _SELFTEST:
        return True
    return not _is_ancestor(rev, ANCHOR_SHA)


# Nur im Selftest aufgeloest — sonst zahlte JEDER Hook-Aufruf git-Subprozesse,
# nur um Testdaten zu bauen. Im Hook-Modus sind die Werte unbenutzt (REPO_CASES
# wird dort nie ausgewertet).
_SELFTEST = len(sys.argv) > 1 and sys.argv[1] == "--selftest"
ANCHOR_SHA = _anchor_rev() if _SELFTEST else ""
MAIN_HISTORY_SHA = _main_history_sha() if _SELFTEST else "HEAD"
NON_ANCESTOR_SHA = _non_ancestor_sha() if _SELFTEST else "-"
ANCHOR_HISTORY_SHA = _anchor_history_sha() if _SELFTEST else "HEAD"
POST_ANCHOR_SHA = _post_anchor_sha() if _SELFTEST else "-"

# (command, cwd, expected) — braucht den echten ~/.claude-Checkout, rein lesend.
REPO_CASES = [
    ("git merge task/foo", CLAUDE_DIR, True),
    ("git checkout task/foo", CLAUDE_DIR, True),
    ("git -C %s merge task/foo" % CLAUDE_DIR, "/tmp", True),
    ("git status", CLAUDE_DIR, False),
    ("git checkout -- scripts/push-gate.py", CLAUDE_DIR, False),
    ("GIT merge task/foo", CLAUDE_DIR, True),
    ("GIT_PAGER=cat GIT merge task/foo", CLAUDE_DIR, True),
    ("GIT status", CLAUDE_DIR, False),
    # GIT_DIR/--git-dir zeigt auf den Hauptcheckout, cwd irgendwohin.
    ("GIT_DIR=%s git merge task/foo" % CLAUDE_GIT_DIR, "/tmp", True),
    ("GIT_DIR=~/.claude/.git git merge task/foo", "/tmp", True),
    ("GIT_DIR=$HOME/.claude/.git git rebase main", "/tmp", True),
    ("git --git-dir=%s merge task/foo" % CLAUDE_GIT_DIR, "/tmp", True),
    ("git --git-dir %s cherry-pick abc1234" % CLAUDE_GIT_DIR, "/tmp", True),
    ("FOO=1 GIT_DIR=%s BAR=2 git reset --hard task/foo" % CLAUDE_GIT_DIR, "/tmp", True),
    ("env GIT_DIR=%s git stash pop" % CLAUDE_GIT_DIR, "/tmp", True),
    ("GIT_DIR=%s git merge task/foo" % CLAUDE_GIT_DIR, "", True),
    ("GIT_DIR=%s git merge task/foo" % CLAUDE_GIT_DIR_CASEVAR, "/tmp",
     os.path.isdir(CLAUDE_GIT_DIR_CASEVAR)),
    # Kombination Case + VAR + GIT_DIR + SUDO-Wrapper.
    ("GIT_TERMINAL_PROMPT=0 GIT_DIR=~/.claude/.git GIT merge task/foo", "/tmp", True),
    ("SUDO GIT_DIR=%s git merge task/foo" % CLAUDE_GIT_DIR, "/tmp", True),
    ("SUDO GIT_TERMINAL_PROMPT=0 GIT_DIR=%s GIT cherry-pick abc1234" % CLAUDE_GIT_DIR,
     "/tmp", True),
    ("GIT_DIR=%s SUDO GIT merge task/foo" % CLAUDE_GIT_DIR, "/tmp", True),
    ('GIT_DIR="%s" git merge task/foo' % CLAUDE_GIT_DIR, "/tmp", True),
    ("FOO=1 BAR=2 GIT_DIR=%s GIT reset --hard task/foo" % CLAUDE_GIT_DIR, "/tmp", True),
    # Relatives git-dir loest git gegen das -C-Ziel auf, nicht gegen den
    # urspruenglichen cwd (gemessen). Erster Fall trifft deshalb nur
    # ueber den gitdir-Pfad — das Home ist kein Repo, die cwd-Pruefung kann dort
    # gar nicht anschlagen. Zweiter Fall zeigt weg und darf nicht treffen.
    ("git --git-dir=.claude/.git merge task/foo", HOME_DIR, True),
    ("git -C /tmp --git-dir=.git merge task/foo", CLAUDE_DIR, False),
    # GIT_DIR auf den Hauptcheckout, aber lesendes Verb -> kein Treffer.
    ("GIT_DIR=%s git status" % CLAUDE_GIT_DIR, "/tmp", False),
    ("GIT_DIR=%s git checkout -- ." % CLAUDE_GIT_DIR, "/tmp", False),
    # Fremdes GIT_DIR / Worktree-GIT_DIR -> nie ein Treffer.
    ("GIT_DIR=/tmp/fremd.git git merge task/foo", "/tmp", False),
    ("git --git-dir=%s/worktrees/xyz merge task/foo" % CLAUDE_GIT_DIR, "/tmp", False),
    # Arbeitsbaum nach ~/.claude umgelenkt, aus einem fremden Repo heraus.
    ("git --work-tree=%s merge task/foo" % CLAUDE_DIR, "/tmp", True),
    ("git --work-tree %s merge task/foo" % CLAUDE_DIR, "/tmp", True),
    ("GIT_WORK_TREE=%s git merge task/foo" % CLAUDE_DIR, "/tmp", True),
    ("GIT_WORK_TREE=~/.claude git rebase main", "/tmp", True),
    ('GIT_WORK_TREE="%s" git merge task/foo' % CLAUDE_DIR, "/tmp", True),
    # Kombination Case + VAR + WORK_TREE (+ SUDO).
    ("GIT_TERMINAL_PROMPT=0 GIT_WORK_TREE=%s GIT merge task/foo" % CLAUDE_DIR, "/tmp", True),
    ("SUDO GIT_WORK_TREE=%s GIT merge task/foo" % CLAUDE_DIR, "/tmp", True),
    ("GIT_WORK_TREE=%s SUDO GIT merge task/foo" % CLAUDE_DIR, "/tmp", True),
    ("FOO=1 GIT_WORK_TREE=%s BAR=2 GIT_DIR=/tmp/fremd.git git merge task/foo" % CLAUDE_DIR,
     "/tmp", True),
    ("GIT_WORK_TREE=%s git merge task/foo" % CLAUDE_DIR_CASEVAR, "/tmp",
     os.path.isdir(CLAUDE_DIR_CASEVAR)),
    # Die sanktionierten Auswege verlieren unter Umlenkung ihre Grundlage.
    ("GIT_WORK_TREE=%s git checkout -- ." % CLAUDE_DIR, "/tmp", True),
    ("GIT_WORK_TREE=%s git checkout -- scripts/push-gate.py" % CLAUDE_DIR, "/tmp", True),
    ("GIT_WORK_TREE=%s git restore -- scripts/x.py" % CLAUDE_DIR, "/tmp", True),
    ("GIT_WORK_TREE=%s git revert --no-edit HEAD" % CLAUDE_DIR, "/tmp", True),
    ("GIT_TERMINAL_PROMPT=0 GIT_WORK_TREE=%s SUDO GIT checkout -- ." % CLAUDE_DIR, "/tmp", True),
    # Unterverzeichnis und uebergeordnetes Verzeichnis treffen genauso (gemessen).
    ("git --work-tree=%s/scripts merge task/foo" % CLAUDE_DIR, "/tmp", True),
    ("git --work-tree=%s/scripts checkout -- gate.py" % CLAUDE_DIR, "/tmp", True),
    ("GIT_WORK_TREE=%s git checkout -- .claude/scripts/x.py" % HOME_DIR, "/tmp", True),
    # Relatives --work-tree gegen das -C-Ziel.
    ("git -C %s --work-tree=.claude merge task/foo" % HOME_DIR, "/tmp", True),
    ("git -C /tmp --work-tree=.claude merge task/foo", CLAUDE_DIR, False),
    # Umlenkung woanders hin, lesendes Verb, fremder Arbeitsbaum -> kein Treffer.
    ("GIT_WORK_TREE=%s git status" % CLAUDE_DIR, "/tmp", False),
    ("GIT_WORK_TREE=%s git log --oneline" % CLAUDE_DIR, "/tmp", False),
    ("git --work-tree=/tmp merge task/foo", "/tmp", False),
    ("GIT_WORK_TREE=/tmp/x git merge task/foo", "/tmp", False),
    # Fremdes Repo / kein Repo / Worktree -> nie ein Treffer.
    ("git merge task/foo", "/tmp", False),
    ("git merge task/foo", "", False),
    ("git merge task/foo", "/nicht/vorhanden", False),
    # ---- fremdes git-dir + impliziter Arbeitsbaum = cwd ----------------------
    # Der Angriff selbst: Task-Branch-Index in die scharfen Dateien.
    ("git --git-dir=%s checkout -- scripts/push-gate.py" % WT_GIT_DIR,
     CLAUDE_DIR, True),
    ("git --git-dir=%s restore -- scripts/push-gate.py" % WT_GIT_DIR,
     CLAUDE_DIR, True),
    ("git --git-dir=%s revert --no-edit HEAD" % WT_GIT_DIR, CLAUDE_DIR, True),
    ("git --git-dir=%s checkout -- ." % WT_GIT_DIR, CLAUDE_DIR, True),
    ("GIT_DIR=%s git checkout -- scripts/x.py" % WT_GIT_DIR, CLAUDE_DIR, True),
    # Asymmetrie-Gegenprobe: merge ist verb-seitig nie erlaubt und bleibt geblockt.
    ("git --git-dir=%s merge task/foo" % WT_GIT_DIR, CLAUDE_DIR, True),
    # Praezedenz-Falle: das Flag gewinnt gegen GIT_DIR= (gemessen), "eine ist
    # eigen" waere hier ein Loch. Beide Reihenfolgen bleiben geblockt.
    ("GIT_DIR=%s git --git-dir=%s checkout -- x" % (CLAUDE_GIT_DIR, WT_GIT_DIR),
     CLAUDE_DIR, True),
    ("GIT_DIR=%s git --git-dir=%s checkout -- x" % (WT_GIT_DIR, CLAUDE_GIT_DIR),
     CLAUDE_DIR, True),
    ("git --git-dir=%s --git-dir=%s checkout -- x" % (CLAUDE_GIT_DIR, WT_GIT_DIR),
     CLAUDE_DIR, True),
    # Voellig fremdes Repo als Quelle (gemessen: schreibt in den cwd).
    ("git --git-dir=/tmp/fremd.git checkout -- scripts/x.py", CLAUDE_DIR, True),
    ("GIT_DIR=/tmp/fremd.git git restore -- scripts/x.py", CLAUDE_DIR, True),
    # cwd im UNTERverzeichnis: der implizite Arbeitsbaum liegt weiter in ~/.claude.
    ("git --git-dir=%s checkout -- x" % WT_GIT_DIR,
     os.path.join(CLAUDE_DIR, "scripts"), True),
    # ueber -C statt cwd.
    ("git -C %s --git-dir=%s checkout -- x" % (CLAUDE_DIR, WT_GIT_DIR), "/tmp", True),
    # Wrapper/Case-Kombination auf demselben Vektor.
    ("SUDO GIT_TERMINAL_PROMPT=0 GIT --git-dir=%s checkout -- ." % WT_GIT_DIR,
     CLAUDE_DIR, True),
    # ---- Gegenrichtung: darf NICHT ueberblocken ------------------------------
    # Der sanktionierte Ausweg, normale Discovery — Regressionsanker.
    ("git checkout -- .", CLAUDE_DIR, False),
    ("git restore -- scripts/x.py", CLAUDE_DIR, False),
    # Haengt am Anker, nicht fest an HEAD (s. _revert_expect).
    ("git revert --no-edit HEAD", CLAUDE_DIR, _revert_expect("HEAD")),
    # Dasselbe Repo, nur redundant explizit angegeben: Quelle UND Ziel sind eigen.
    ("git --git-dir=%s checkout -- ." % CLAUDE_GIT_DIR, CLAUDE_DIR, False),
    ("GIT_DIR=%s git checkout -- ." % CLAUDE_GIT_DIR, CLAUDE_DIR, False),
    ("GIT_DIR=~/.claude/.git git restore -- scripts/x.py", CLAUDE_DIR, False),
    ("git --git-dir=%s revert --no-edit HEAD" % CLAUDE_GIT_DIR, CLAUDE_DIR,
     _revert_expect("HEAD")),
    # Case-Variante des EIGENEN gitdir: auf case-insensitivem FS ist es dieselbe
    # Datei (-> eigener Index -> erlaubt), sonst existiert der Pfad nicht
    # (-> nicht als eigen nachweisbar -> geblockt). Erwartung aus dem FS abgeleitet.
    ("git --git-dir=%s checkout -- ." % CLAUDE_GIT_DIR_CASEVAR, CLAUDE_DIR,
     not os.path.isdir(CLAUDE_GIT_DIR_CASEVAR)),
    # Lesendes Verb bleibt lesend, egal welches git-dir.
    ("git --git-dir=%s status" % WT_GIT_DIR, CLAUDE_DIR, False),
    ("git --git-dir=%s log --oneline" % WT_GIT_DIR, CLAUDE_DIR, False),
    # Fremdes git-dir UND fremder Arbeitsbaum: konsistent fremd -> nicht in Scope.
    ("git --git-dir=%s checkout -- x" % WT_GIT_DIR, "/tmp", False),
    ("git --git-dir=/tmp/fremd.git checkout -- x", "/tmp", False),
    # Eigener Index in einen FREMDEN Arbeitsbaum: schreibt nach /tmp, nicht in die
    # Live-Dateien -> bleibt erlaubt.
    ("GIT_DIR=%s git checkout -- ." % CLAUDE_GIT_DIR, "/tmp", False),
    # ---- Optionen mit separatem Argument, mit Repo-Kontext -------------------
    ("git --shallow-file status merge --no-edit task/x", CLAUDE_DIR, True),
    ("git --attr-source main merge task/foo", CLAUDE_DIR, True),
    ("git --frobnicate status", CLAUDE_DIR, True),
    ("git --shallow-file status merge task/x", "/tmp", False),
    ("git --no-pager log --oneline", CLAUDE_DIR, False),
    ("git --namespace ns status", CLAUDE_DIR, False),
    # ---- Fuehrende Umlenkung, mit Repo-Kontext: der Praefix-Bypass, scharf ---
    # Die einfachste Form: ein Praefix, das den ganzen Gate aushebeln wuerde.
    ("2>/dev/null git merge task/foo", CLAUDE_DIR, True),
    ("2> /dev/null git merge task/foo", CLAUDE_DIR, True),
    ("2>&1 git merge task/foo", CLAUDE_DIR, True),
    ("&>/tmp/x git rebase main", CLAUDE_DIR, True),
    ("&>>/tmp/x git checkout task/foo", CLAUDE_DIR, True),
    (">/tmp/x GIT merge task/foo", CLAUDE_DIR, True),
    # Kombination Umlenkung + Case + Wrapper + GIT_DIR/--work-tree auf ~/.claude.
    ("2>/dev/null GIT_DIR=%s SUDO GIT merge task/foo" % CLAUDE_GIT_DIR, "/tmp", True),
    ("2> /dev/null git --work-tree=%s merge task/foo" % CLAUDE_DIR, "/tmp", True),
    ("2>&1 GIT_WORK_TREE=%s GIT checkout -- ." % CLAUDE_DIR, "/tmp", True),
    ("2>/dev/null git --git-dir=%s checkout -- scripts/x.py" % WT_GIT_DIR, CLAUDE_DIR, True),
    # Gegenrichtung: kein Ueberblocken.
    ("2>/dev/null git status", CLAUDE_DIR, False),
    ("2>&1 git log --oneline", CLAUDE_DIR, False),
    ("2>/dev/null git checkout -- .", CLAUDE_DIR, False),
    ("2>/dev/null git merge task/foo", "/tmp", False),
    ("npm test & git merge task/foo", CLAUDE_DIR, True),
    # Shell verschluckt das Kommandowort -> kein git-Aufruf, nichts zu blocken.
    ("> git merge task/foo", CLAUDE_DIR, False),
    # ---- revert nur fuer die Historie des eingefrorenen Ankers ----------------
    # Der Waesche-Angriff: Cleanup-Commit aus einem Worktree, per SHA aus dem
    # Hauptcheckout erreichbar, aber KEIN Ancestor des Ankers -> DENY.
    ("git revert --no-edit %s" % NON_ANCESTOR_SHA, CLAUDE_DIR, True),
    ("git revert %s" % NON_ANCESTOR_SHA, CLAUDE_DIR, True),
    ("git revert -m 1 %s" % NON_ANCESTOR_SHA, CLAUDE_DIR, True),
    ("git revert abc1234", CLAUDE_DIR, True),          # nicht aufloesbar -> rc=128
    ("git revert task/foo", CLAUDE_DIR, True),         # Branch-Tip ist kein Ancestor
    ("git revert", CLAUDE_DIR, True),                  # kein Ziel bestimmbar
    # KERNFALL des Ankers: ein Ziel, das Ancestor von HEAD ist, aber NICHT des
    # Ankers. Genau das stellt die Ref-Verschiebung her, und genau das liesse
    # ein HEAD-Vergleich durch
    # (im Wegwerf-Repo mit CLAUDE_DIR-Umbiegung beidseitig gemessen:
    # gegen HEAD rc=0/pass, gegen den Anker rc=1/block). Steht der Anker auf
    # HEAD, faellt der Wert auf NON_ANCESTOR_SHA zurueck — DENY bleibt DENY.
    ("git revert --no-edit %s" % POST_ANCHOR_SHA, CLAUDE_DIR, True),
    ("git revert %s" % POST_ANCHOR_SHA, CLAUDE_DIR, True),
    ("2>/dev/null GIT revert --no-edit %s" % POST_ANCHOR_SHA, CLAUDE_DIR, True),
    ("GIT_DIR=%s git revert %s" % (CLAUDE_GIT_DIR, POST_ANCHOR_SHA), "/tmp", True),
    # Ueber die subprozessfreien Scopes — derselbe Pfad, den die Fault-Injection
    # nutzt (bei fehlendem git-Binary wird der cwd-Scope gar nicht erst erkannt).
    ("GIT_DIR=%s git revert --no-edit %s" % (CLAUDE_GIT_DIR, NON_ANCESTOR_SHA), "/tmp", True),
    ("git --git-dir=%s revert %s" % (CLAUDE_GIT_DIR, NON_ANCESTOR_SHA), CLAUDE_DIR, True),
    # Kombinationsfaelle: Umlenkung + revert, Umlenkung + Case-Variante.
    ("2>/dev/null git revert --no-edit %s" % NON_ANCESTOR_SHA, CLAUDE_DIR, True),
    ("2>&1 GIT revert %s" % NON_ANCESTOR_SHA, CLAUDE_DIR, True),
    ("2> /dev/null SUDO GIT revert abc1234", CLAUDE_DIR, True),
    # Der sanktionierte Ausweg bleibt offen — aber nur fuer die Historie des
    # ANKERS. Alle diese Erwartungen sind deshalb aus der Umgebung abgeleitet
    # (_revert_expect): ohne gesetzten Anker blockt der Gate sie fail-closed mit,
    # und genau das soll der Selftest zeigen statt es zu verstecken.
    ("git revert HEAD", CLAUDE_DIR, _revert_expect("HEAD")),
    ("git revert %s" % MAIN_HISTORY_SHA, CLAUDE_DIR, _revert_expect(MAIN_HISTORY_SHA)),
    ("git revert --no-edit -m 1 %s" % MAIN_HISTORY_SHA, CLAUDE_DIR,
     _revert_expect(MAIN_HISTORY_SHA)),
    ("git revert -n HEAD", CLAUDE_DIR, _revert_expect("HEAD")),
    ("git revert -S HEAD", CLAUDE_DIR, _revert_expect("HEAD")),  # -S frisst kein Folgetoken
    ("2>/dev/null GIT revert --no-edit HEAD", CLAUDE_DIR, _revert_expect("HEAD")),
    ("GIT_DIR=%s git revert --no-edit HEAD" % CLAUDE_GIT_DIR, "/tmp",
     _revert_expect("HEAD")),
    # Ein Ziel AUS der Anker-Historie bleibt erlaubt — der Gegenbeweis zum
    # Kernfall oben, sonst waere "blockt alles" trivial gruen.
    ("git revert --no-edit %s" % ANCHOR_HISTORY_SHA, CLAUDE_DIR,
     _revert_expect(ANCHOR_HISTORY_SHA)),
    ("git revert %s" % ANCHOR_REF, CLAUDE_DIR, _revert_expect(ANCHOR_REF)),
    # Formen ohne Zielcommit ziehen nichts herein und laufen deshalb AUCH OHNE
    # gesetzten Anker durch — die Ziel-Pruefung steht bewusst vor der
    # Anker-Meldung, sonst blockte ein nicht gebootstrappter Anker das Abbrechen
    # eines laufenden revert. Fest verdrahtet, nicht abgeleitet: genau diese
    # Unabhaengigkeit vom Anker soll der Fall festhalten.
    ("git revert --abort", CLAUDE_DIR, False),
    ("git revert --continue", CLAUDE_DIR, False),
    ("git revert --quit", CLAUDE_DIR, False),
    # Fremdes Repo: weder Anker- noch Ancestor-Check laufen an.
    ("git revert --no-edit %s" % NON_ANCESTOR_SHA, "/tmp", False),
    ("git revert abc1234", "/tmp", False),
    ("git revert HEAD", "/tmp", False),
    # ---- Index-only-Formen von restore/reset, mit Repo-Kontext ---------------
    # Die legitimen Formen, im Hauptcheckout und ueber -C.
    ("git restore --staged scripts/merge-gate.py", CLAUDE_DIR, False),
    ("git restore --staged -- scripts/merge-gate.py", CLAUDE_DIR, False),
    ("git reset HEAD -- scripts/merge-gate.py", CLAUDE_DIR, False),
    ("git reset -- scripts/merge-gate.py", CLAUDE_DIR, False),
    ("git -C %s restore --staged scripts/merge-gate.py" % CLAUDE_DIR, "/tmp", False),
    ("git -C %s reset HEAD -- scripts/merge-gate.py" % CLAUDE_DIR, "/tmp", False),
    # Der zentrale Beweis: unter Arbeitsbaum-Umlenkung und unter fremdem
    # git-dir mit implizitem Arbeitsbaum bleiben die Index-only-Formen
    # erlaubt — sie schreiben dort nachweislich keine Datei (gemessen).
    ("GIT_WORK_TREE=%s git restore --staged scripts/x.py" % CLAUDE_DIR, "/tmp", False),
    ("git --work-tree=%s restore --staged -- scripts/x.py" % CLAUDE_DIR, "/tmp", False),
    ("GIT_WORK_TREE=%s git reset HEAD -- scripts/x.py" % CLAUDE_DIR, "/tmp", False),
    ("git --work-tree=%s reset -- scripts/x.py" % CLAUDE_DIR, "/tmp", False),
    ("git --git-dir=%s restore --staged scripts/x.py" % WT_GIT_DIR, CLAUDE_DIR, False),
    ("git --git-dir=%s reset HEAD -- scripts/x.py" % WT_GIT_DIR, CLAUDE_DIR, False),
    ("GIT_DIR=%s git restore --staged scripts/x.py" % WT_GIT_DIR, CLAUDE_DIR, False),
    ("2>/dev/null git restore --staged scripts/x.py", CLAUDE_DIR, False),
    # Gegenprobe zur Ausnahme: sobald der Arbeitsbaum mitgeschrieben wird oder
    # die Quelle fremd ist, greifen Veto bzw. Formpruefung unveraendert.
    ("GIT_WORK_TREE=%s git restore --staged --worktree -- scripts/x.py" % CLAUDE_DIR,
     "/tmp", True),
    ("GIT_WORK_TREE=%s git restore -SW -- scripts/x.py" % CLAUDE_DIR, "/tmp", True),
    ("git --git-dir=%s restore --staged --worktree -- scripts/x.py" % WT_GIT_DIR,
     CLAUDE_DIR, True),
    ("GIT_WORK_TREE=%s git reset task/foo -- scripts/x.py" % CLAUDE_DIR, "/tmp", True),
    # `--staged` HINTER dem `--` ist ein Pfadname, keine Index-only-Form: unter
    # Umlenkung schreibt es die Datei (gemessen) und muss weiter ans Veto fallen.
    ("GIT_WORK_TREE=%s git restore -- --staged" % CLAUDE_DIR, "/tmp", True),
    ("git --git-dir=%s restore -- --staged" % WT_GIT_DIR, CLAUDE_DIR, True),
    # ---- Praefix-Abkuerzungen, mit Repo-Kontext: der Exploit, scharf ----------
    # Praefix-Abkuerzung von --worktree unter Umlenkung: genau der Laundering-Pfad
    # (gemessen: schreibt die Datei).
    ("GIT_WORK_TREE=%s git restore --staged --wor -- scripts/push-gate.py"
     % CLAUDE_DIR, "/tmp", True),
    ("GIT_WORK_TREE=%s git restore --staged --work -- scripts/x.py" % CLAUDE_DIR,
     "/tmp", True),
    ("git --work-tree=%s restore --staged --wor -- scripts/x.py" % CLAUDE_DIR,
     "/tmp", True),
    ("git --git-dir=%s restore --staged --wor -- scripts/push-gate.py"
     % WT_GIT_DIR, CLAUDE_DIR, True),
    ("GIT_WORK_TREE=%s git restore --sta --wor -- scripts/x.py" % CLAUDE_DIR,
     "/tmp", True),
    ("2>/dev/null GIT_WORK_TREE=%s SUDO GIT restore --staged --wor -- scripts/x.py"
     % CLAUDE_DIR, "/tmp", True),
    # --source-Praefix vergiftet den Index auch ohne jede Umlenkung.
    ("git restore --sou=task/foo -- scripts/x.py", CLAUDE_DIR, True),
    ("git restore --so=task/foo -- scripts/x.py", CLAUDE_DIR, True),
    ("git restore --staged --sou=task/foo -- scripts/x.py", CLAUDE_DIR, True),
    ("git restore --sourc task/foo -- scripts/x.py", CLAUDE_DIR, True),
    # Gegenprobe: die Index-only-Form bleibt trotz der Verschaerfung erlaubt.
    ("GIT_WORK_TREE=%s git restore --staged -- scripts/x.py" % CLAUDE_DIR, "/tmp", False),
    ("git restore -S -- scripts/x.py", CLAUDE_DIR, False),
    # Pathspec nach `--`, das wie ein Flag aussieht: kein Flag.
    ("git restore --staged -- -sfile", CLAUDE_DIR, False),
    ("git restore --staged -- --source=x", CLAUDE_DIR, False),
    ("git reset task/foo -- scripts/x.py", CLAUDE_DIR, True),
    ("git reset --hard -- scripts/x.py", CLAUDE_DIR, True),
    ("git restore --staged --source=task/foo -- scripts/x.py", CLAUDE_DIR, True),
    # ---- Kurzflag-Cluster, mit Repo-Kontext: scharf --------------------------
    ("git restore -sother -- scripts/x.py", CLAUDE_DIR, True),
    ("git restore -qsother -- scripts/x.py", CLAUDE_DIR, True),
    ("git restore -qs task/foo -- scripts/x.py", CLAUDE_DIR, True),
    ("git checkout -qs task/foo -- scripts/x.py", CLAUDE_DIR, True),
    # ---- Kontrollfluss-Woerter, mit Repo-Kontext: der False Positive ---------
    ("python3 -c \"\nfor cmd in cmds:\n    if 'git' in cmd.lower() or x:\n        pass\n\"",
     CLAUDE_DIR, False),
    ("if 'git' in cmd.lower() or x:", CLAUDE_DIR, False),
    ("! git merge task/foo", CLAUDE_DIR, True),
    # ---- Kontrollfluss vor nacktem git, mit Repo-Kontext: der Schutz bleibt --
    ("if git merge task/foo; then echo done; fi", CLAUDE_DIR, True),
    ("while git merge task/foo; do :; done", CLAUDE_DIR, True),
    ("until git rebase main; do :; done", CLAUDE_DIR, True),
    ("if ! git merge task/foo; then :; fi", CLAUDE_DIR, True),
    ("if git status; then git merge task/foo; fi", CLAUDE_DIR, True),
    ("if gh pr checkout 12; then :; fi", CLAUDE_DIR, True),
    # Gegenprobe Scope und harmloser Code.
    ("if git merge task/foo; then echo done; fi", "/tmp", False),
    ("if git status; then echo ok; fi", CLAUDE_DIR, False),
    ("if git_status == 'clean': pass", CLAUDE_DIR, False),
    # ---- `remote -v` bleibt lesend, auch scope-seitig (CASES prueft nur das
    # Verb).
    ("git remote -v", CLAUDE_DIR, False),
    ("git -C %s remote -v" % CLAUDE_DIR, "/tmp", False),
    ("git remote show origin", CLAUDE_DIR, False),
    ("git remote get-url origin", CLAUDE_DIR, False),
    ("git remote add x /tmp/y", CLAUDE_DIR, True),
    # ---- Listenformen mit Repo-Scope: dieselben Formen wie in CASES, aber im echten
    # Hauptcheckout. CASES prueft nur das Verb, hier haengt die Einordnung
    # zusaetzlich am Ziel-Scope.
    ("git merge-base main HEAD", CLAUDE_DIR, False),
    ("git merge-base --is-ancestor HEAD main", CLAUDE_DIR, False),
    ("git branch -a --contains HEAD", CLAUDE_DIR, False),
    ("git branch --list 'task/*'", CLAUDE_DIR, False),
    ("git branch -l 'task/*'", CLAUDE_DIR, False),
    ("git tag --contains HEAD", CLAUDE_DIR, False),
    ("git tag -l 'v*'", CLAUDE_DIR, False),
    ("git branch -d task/foo", CLAUDE_DIR, True),
    ("git branch -u origin/main", CLAUDE_DIR, True),
    ("git branch --set-upstream-to=origin/main", CLAUDE_DIR, True),
    ("git branch --edit-description", CLAUDE_DIR, True),
    ("git tag -d v1.0", CLAUDE_DIR, True),
    ("git remote remove origin", CLAUDE_DIR, True),
    ("git remote set-url origin /tmp/y", CLAUDE_DIR, True),
    ("git remote rename origin upstream", CLAUDE_DIR, True),
    ("git remote prune origin", CLAUDE_DIR, True),
    ("git remote update", CLAUDE_DIR, True),
    ("git worktree add /tmp/w task/foo", CLAUDE_DIR, True),
    ("git worktree move /tmp/w /tmp/w2", CLAUDE_DIR, True),
    ("git worktree lock /tmp/w", CLAUDE_DIR, True),
    ("git worktree prune", CLAUDE_DIR, True),
    ("git worktree repair", CLAUDE_DIR, True),
    # ---- Ref-Verschiebung bleibt bewusst offen ----------------
    # Diese drei Faelle dokumentieren den Ist-Zustand als ASSERTION, damit eine
    # kuenftige Verengung der Index-only-Ausnahme oder des revert-Checks hier
    # sichtbar anschlaegt statt still zu passieren. Sie sind KEINE Freigabe — die
    # Begruendung steht im Docstring.
    # Schritt 2 der Kette, in beiden Formen, ohne jede Umlenkung:
    ("git restore --staged scripts/push-gate.py", CLAUDE_DIR, False),
    ("git reset HEAD -- scripts/push-gate.py", CLAUDE_DIR, False),
    # Schritt 2' der revert-Variante: gegen HEAD bestuende ein STRIKTER Ancestor
    # von HEAD die Pruefung — nach der Ref-Verschiebung war das der
    # Angreifer-Commit (gemessen). Die Pruefung haengt am eingefrorenen Anker;
    # im SAUBEREN Repo (Anker == HEAD) bleibt der Fall erlaubt, nach einem Hijack
    # faellt er. Die Erwartung ist deshalb abgeleitet — der scharfe Gegenfall
    # steht als POST_ANCHOR_SHA im Anker-Block.
    ("git revert --no-edit %s" % MAIN_HISTORY_SHA, CLAUDE_DIR,
     _revert_expect(MAIN_HISTORY_SHA)),
    # ---- gh pr checkout --------------------------------
    # Schaltet den Arbeitsbaum auf den PR-Branch um. Der Scope ist reiner cwd:
    # gh kennt weder -C noch --git-dir/--work-tree.
    ("gh pr checkout 12", CLAUDE_DIR, True),
    ("gh pr co 12", CLAUDE_DIR, True),                 # eingebauter gh-Alias
    ("gh co 12", CLAUDE_DIR, True),                    # Nutzer-Alias
    ("gh PR CHECKOUT 12", CLAUDE_DIR, True),
    ("gh pr checkout https://github.com/o/r/pull/32", CLAUDE_DIR, True),
    ("gh pr checkout feature-branch", CLAUDE_DIR, True),
    ("gh pr checkout", CLAUDE_DIR, True),              # interaktive Auswahl
    ("gh pr checkout --detach 12", CLAUDE_DIR, True),
    ("gh pr checkout -f 12", CLAUDE_DIR, True),
    ("gh pr checkout --recurse-submodules 12", CLAUDE_DIR, True),
    # -R waehlt das REMOTE-Repo; der Arbeitsbaum bleibt der des cwd -> blocken.
    ("gh pr checkout -R other/repo 12", CLAUDE_DIR, True),
    ("gh pr checkout --repo other/repo 12", CLAUDE_DIR, True),
    # -b nimmt einen WERT: der darf nicht als Positional gelesen werden.
    ("gh pr checkout -b checkout 12", CLAUDE_DIR, True),
    # Die Praefix-Behandlung (Umlenkung, Wrapper) muss auch fuer gh greifen.
    ("2>/dev/null gh co 12", CLAUDE_DIR, True),
    ("noglob gh pr checkout 12", CLAUDE_DIR, True),
    ("sudo gh pr checkout 12", CLAUDE_DIR, True),
    ("echo x && gh co 12", CLAUDE_DIR, True),
    # Scope-Gegenprobe: ausserhalb des Hauptcheckouts unberuehrt.
    ("gh pr checkout 12", "/tmp", False),
    ("gh co 12", "/tmp", False),
    # Andere gh-Subkommandos bewegen den Arbeitsbaum nicht — dieser Gate darf
    # sie nicht zusaetzlich blocken.
    ("gh pr list", CLAUDE_DIR, False),
    ("gh pr view 12", CLAUDE_DIR, False),
    ("gh pr create --fill", CLAUDE_DIR, False),
    ("gh pr merge 12", CLAUDE_DIR, False),
    ("gh repo view", CLAUDE_DIR, False),
    ("gh alias list", CLAUDE_DIR, False),
    ("gh issue list", CLAUDE_DIR, False),
    ("echo 'gh pr checkout 12'", CLAUDE_DIR, False),
    ("grep -rn 'gh pr checkout' scripts/", CLAUDE_DIR, False),
]

# (command, cwd) — der cwd gehoert zwingend dazu: wird alles mit cwd=CLAUDE_DIR
# geprueft, meldet schon die cwd-Pruefung einen Treffer und eine Luecke, die
# eigentlich nur ueber einen fremden cwd laeuft, sieht faelschlich geschlossen aus.
KNOWN_GAPS = [
    ("alias gm='git merge'; gm task/foo", CLAUDE_DIR),
    ("bash -c \"git merge task/foo\"", CLAUDE_DIR),
    ("python -c \"import subprocess; subprocess.run(['git','merge','task/foo'])\"", CLAUDE_DIR),
    # Wrapper mit eigenen Flags: der Praefix-Scan bricht am ersten Flag ab, das
    # kein NAME=WERT und kein Wrapper-Wort ist. Klasse, nicht Einzelfall.
    ("env -i GIT_DIR=%s git merge task/foo" % CLAUDE_GIT_DIR, "/tmp"),
    ("sudo -u alice git merge task/foo", CLAUDE_DIR),
    # Ref-Verschiebung ueber ein Worktree-git-dir. Bewusst nicht gefixt: aus
    # dem Worktree selbst mit normaler Discovery identisch erreichbar, und
    # Worktrees laesst dieser Gate absichtlich frei (Docstring, Abschnitt zur
    # Ref-Verschiebung).
    ("git --git-dir=%s update-ref refs/heads/main abc1234" % WT_GIT_DIR, "/tmp"),
    # Die Folgeschritte, jeder fuer sich sanktioniert.
    # Sie stehen hier NICHT als neue Luecke, sondern damit die Kette an einer
    # Stelle vollstaendig sichtbar ist — Schritt 2 der Index-only-Variante und
    # Schritt 2' der revert-Variante, die die Index-only-Ausnahme gar nicht
    # braucht.
    ("git restore --staged scripts/push-gate.py", CLAUDE_DIR),
    # DIESE Zeile ist der Teil der Kette, den der Anker
    # schliesst — im sauberen Repo (Anker == HEAD) laeuft sie weiter durch, nach
    # der Ref-Verschiebung nicht mehr. Die Index-only-Variante darueber bleibt
    # vollstaendig offen, sie fragt keinen Anker.
    ("git revert --no-edit %s" % MAIN_HISTORY_SHA, CLAUDE_DIR),
    # Restluecke der Anker-Isolation, im Wegwerf-Repo scharf gemessen: der
    # `main-worktree/`-Praefix laesst einen Worktree die Anker-Kopie des
    # HAUPTCHECKOUTS direkt schreiben (rc=0, Datei .git/refs/worktree/… traegt
    # danach den fremden SHA). Der Anker ist damit aus einem Worktree
    # faelschbar — ein zusaetzliches, unuebliches Kommando, keine geschlossene
    # Tuer. Gemessen in allen vier Aufrufformen: `-C <wt>`, cwd=<wt>,
    # `--git-dir=<wt-admindir>` und `update-ref --stdin`.
    ("git --git-dir=%s update-ref main-worktree/%s abc1234" % (WT_GIT_DIR, ANCHOR_REF),
     "/tmp"),
    # Restklasse: `{fd}>f` (bash >= 4.1). In der Ziel-Umgebung nicht
    # ausnutzbar — gemessen fuehrt weder zsh 5.9 noch bash 3.2.57 das Folgewort
    # aus; zusaetzlich frisst _strip_wrappers das fuehrende `{`.
    ("{fd}>/tmp/f git merge task/foo", CLAUDE_DIR),
    # Restklasse: gh-SHELL-Aliase (`gh alias set x '!…'`) fuehren beliebigen
    # Shell-Code aus; _gh_checkout_aliases ueberspringt sie bewusst, weil ihr
    # Inhalt hier nicht interpretierbar ist. Gleiche Klasse wie `bash -c`.
    ("gh shellalias 12", CLAUDE_DIR),
    # Restkante: nach dem Kontrollwort steht kein NACKTES `git`, sondern eine
    # pfad-/quote-qualifizierte Form. Bewusst offen — Basename-Aufloesung an
    # dieser Stelle wuerde harmlose Zeilen wie `if src/git in paths:` blocken.
    ("if /usr/bin/git merge task/foo; then echo done; fi", CLAUDE_DIR),
]

# (payload, erwarteter Rueckgabewert von _cwd_of). "" = kein cwd mitgegeben
# (nichts zu bestimmen, regulaerer Pfad), None = unauswertbar -> deny.
CWD_CASES = [
    ({}, ""),
    ({"cwd": None}, ""),
    ({"cwd": ""}, ""),
    ({"cwd": CLAUDE_DIR}, CLAUDE_DIR),
    ({"cwd": "/tmp"}, "/tmp"),
    ({"cwd": 123}, None),
    # Falsy und trotzdem kein String: ein `data.get("cwd") or ""` machte daraus
    # stillschweigend "" und lief mit unbestimmtem Scope weiter.
    ({"cwd": 0}, None),
    ({"cwd": False}, None),
    ({"cwd": True}, None),
    ({"cwd": ["/tmp"]}, None),
    ({"cwd": {"path": "/tmp"}}, None),
]


# JSON-SHAPE + cwd END-TO-END. Die vier anderen Gruppen pruefen evaluate(),
# das Parsing und _cwd_of ISOLIERT. Keine von ihnen ruft main() auf. Damit
# bliebe der gesamte stdin/JSON-Rand ungetestet: die Top-Level-Shape (ist die
# Nutzlast ueberhaupt ein dict?), die tool_input-Shape, und die VERDRAHTUNG des
# cwd-Checks. An diesem Rand wuerde eine Exception den Hook abstuerzen lassen,
# und ein abgestuerzter PreToolUse-Hook laesst das Tool laufen (fail-open).
# Deshalb wird hier ein echter Subprozess auf DIESE Datei angesetzt und die
# Nutzlast ueber stdin gepiped.
#
# Abgrenzung zu CWD_CASES, damit nichts doppelt getestet wird: CWD_CASES prueft
# die REINE FUNKTION _cwd_of ueber alle elf Shape-Varianten. Hier stehen nur die
# Faelle, die eine Unit-Pruefung von _cwd_of prinzipiell NICHT zeigen kann —
# dass der Rueckgabewert in main() auch wirklich verdrahtet ist (deny statt
# Weiterlaufen), mit welchem GRUND geblockt wird, in welcher REIHENFOLGE
# command- und cwd-Pruefung greifen, und dass ein gueltiger cwd tatsaechlich bis
# in evaluate() durchschlaegt (identisches Kommando, zwei cwds, zwei Ergebnisse).
#
# (label, stdin_text, erwartete Klasse) mit den Klassen:
#   deny-unparseable     — _deny_unparseable() mit UNPARSEABLE_REASON (Kommando)
#   deny-unparseable-cwd — _deny_unparseable() mit UNPARSEABLE_CWD_REASON
#   deny-gate            — regulaerer Treffer der Detection
#   allow                — Exit 0, KEIN Output (nichts zu pruefen)
E2E_TIMEOUT = 30.0
def _e2e_payload(command, cwd=CLAUDE_DIR):
    return json.dumps({"tool_input": {"command": command}, "cwd": cwd})


E2E_CASES = [
    # --- Top-Level-Shape der Nutzlast ---
    ("top-level JSON-Array statt Objekt", "[]", "deny-unparseable"),
    ("top-level JSON-String statt Objekt", '"x"', "deny-unparseable"),
    ("top-level JSON-Zahl statt Objekt", "42", "deny-unparseable"),
    # --- tool_input-Shape ---
    # Gemessen, nicht angenommen: die LEERE Liste laeuft NICHT in den Deny.
    # `_command_of` macht `data.get("tool_input") or {}`, und eine leere Liste ist
    # falsy -> sie wird zum leeren dict, `command` ist "" und der Aufruf gilt als
    # "nichts zu pruefen". Harmlos (eine leere Liste traegt kein Kommando), aber
    # es ist NICHT der Deny-Pfad. Hier als Ist-Zustand festgeschrieben statt
    # eine Wunschbehauptung zu testen; der scharfe Fall steht direkt darunter.
    ("tool_input ist eine LEERE Liste (falsy -> nichts zu pruefen)",
     '{"tool_input": []}', "allow"),
    ("tool_input ist eine nicht-leere Liste",
     '{"tool_input": ["git", "merge", "task/foo"]}', "deny-unparseable"),
    ("tool_input ist ein String", '{"tool_input": "git merge task/foo"}',
     "deny-unparseable"),
    ("command ist ein int", '{"tool_input": {"command": 123}}', "deny-unparseable"),
    ("command ist eine Liste",
     '{"tool_input": {"command": ["git", "merge", "task/foo"]}}', "deny-unparseable"),
    # --- Regressionswaechter: der legitime "nichts zu pruefen"-Pfad (command == "")
    # darf NICHT in den Deny laufen, sonst blockt der Gate jedes fremde Tool. ---
    ("leeres Objekt ohne tool_input", "{}", "allow"),
    ("tool_input ist null", '{"tool_input": null}', "allow"),
    ("command ist der leere String", '{"tool_input": {"command": ""}}', "allow"),
    ("harmloser String-Command",
     '{"tool_input": {"command": "git status"}, "cwd": "%s"}' % CLAUDE_DIR, "allow"),
    # Gegenprobe: ein Fall aus CASES/REPO_CASES muss weiterhin durch die
    # Detection blocken — der Shape-Guard darf echte Treffer nicht verschlucken.
    ("geblockter Fall aus CASES (git merge)",
     '{"tool_input": {"command": "git merge task/foo"}, "cwd": "%s"}' % CLAUDE_DIR,
     "deny-gate"),
    # Kaputtes JSON ist an dieser Stelle BEWUSST fail-open (`except: sys.exit(0)`
    # beim json.load) — hier nur festgeschrieben, nicht geaendert.
    ("kaputtes JSON (bewusst fail-open)", "das{ist:kein}json", "allow"),
    # --- Listenformen ueber den echten main()-Pfad: Leseform durch, Mutation blockt. ---
    ("Listenform durch: merge-base", _e2e_payload("git merge-base main HEAD"), "allow"),
    ("Listenform durch: branch -a --contains",
     _e2e_payload("git branch -a --contains HEAD"), "allow"),
    ("Listenform durch: branch --list <muster>",
     _e2e_payload("git branch --list task/*"), "allow"),
    ("Listenform durch: tag -l <muster>", _e2e_payload("git tag -l v*"), "allow"),
    ("Mutation block: branch -d", _e2e_payload("git branch -d task/foo"), "deny-gate"),
    ("Mutation block: worktree prune", _e2e_payload("git worktree prune"), "deny-gate"),

    # --- cwd END-TO-END: der cwd-Check ueber den echten main()-Pfad. ---
    # Der eigentliche Nachweis, den CWD_CASES nicht fuehren kann: ein
    # nicht-stringartiger cwd erreicht evaluate() gar nicht mehr, sondern faellt
    # in _deny_unparseable — UND zwar mit dem cwd-eigenen Grund, nicht mit dem
    # Kommando-Grund. Das Kommando ist hier bewusst HARMLOS (`git status`): wuerde
    # der cwd-Check nicht verdrahtet sein, waere das Ergebnis `allow`.
    ("cwd ist ein int (harmloses Kommando)",
     '{"tool_input": {"command": "git status"}, "cwd": 123}',
     "deny-unparseable-cwd"),
    ("cwd ist ein dict",
     '{"tool_input": {"command": "git status"}, "cwd": {"path": "/tmp"}}',
     "deny-unparseable-cwd"),
    ("cwd ist eine Liste",
     '{"tool_input": {"command": "git status"}, "cwd": ["/tmp"]}',
     "deny-unparseable-cwd"),
    # Der Kern des cwd-Checks: FALSY und trotzdem kein String. Ein
    # `data.get("cwd") or ""` machte daraus stillschweigend den leeren cwd und
    # lief mit unbestimmtem Scope weiter — hier muss es denyen.
    ("cwd ist false (falsy, aber kein String)",
     '{"tool_input": {"command": "git status"}, "cwd": false}',
     "deny-unparseable-cwd"),
    ("cwd ist 0 (falsy, aber kein String)",
     '{"tool_input": {"command": "git status"}, "cwd": 0}',
     "deny-unparseable-cwd"),
    ("cwd ist true",
     '{"tool_input": {"command": "git status"}, "cwd": true}',
     "deny-unparseable-cwd"),
    # Reihenfolge: main() prueft ERST das Kommando, DANN den cwd. Sind beide
    # kaputt, muss der Kommando-Grund gewinnen — sonst waere die Diagnose im Log
    # irrefuehrend. Festgeschrieben, damit ein Umstellen auffaellt.
    ("command UND cwd kaputt -> Kommando-Grund gewinnt",
     '{"tool_input": {"command": 123}, "cwd": 123}', "deny-unparseable"),
    # Regressionswaechter fuer die legitimen cwd-Formen: kein Deny.
    ("cwd fehlt ganz, harmloses Kommando",
     '{"tool_input": {"command": "git status"}}', "allow"),
    ("cwd ist null, harmloses Kommando",
     '{"tool_input": {"command": "git status"}, "cwd": null}', "allow"),
    ("cwd ist der leere String, harmloses Kommando",
     '{"tool_input": {"command": "git status"}, "cwd": ""}', "allow"),
    # Und der Nachweis, dass ein GUELTIGER cwd nicht nur akzeptiert, sondern auch
    # ausgewertet wird: identisches Kommando, zwei cwds, zwei Ergebnisse. Ohne
    # dieses Paar wuerde ein main(), das den cwd-Rueckgabewert wegwirft und immer
    # "" weiterreicht, alle Faelle oben trotzdem bestehen.
    ("gueltiger cwd ~/.claude schlaegt bis evaluate() durch",
     '{"tool_input": {"command": "git merge task/foo"}, "cwd": "%s"}' % CLAUDE_DIR,
     "deny-gate"),
    ("gueltiger cwd /tmp -> ausserhalb des Scope, kein Treffer",
     '{"tool_input": {"command": "git merge task/foo"}, "cwd": "/tmp"}', "allow"),
]


def _run_gate(payload):
    proc = subprocess.run([sys.executable, os.path.abspath(__file__)],
                          input=payload, capture_output=True, text=True,
                          timeout=E2E_TIMEOUT)
    return proc.returncode, proc.stdout.strip()


def _classify_gate_output(rc, out):
    if rc != 0:
        return f"exit-{rc}"
    if not out:
        return "allow"
    try:
        hook = json.loads(out)["hookSpecificOutput"]
        decision = hook["permissionDecision"]
        reason = hook["permissionDecisionReason"]
    except Exception:
        return "unlesbarer-output"
    if decision != "deny":
        return decision
    if UNPARSEABLE_CWD_REASON in reason:
        return "deny-unparseable-cwd"
    if UNPARSEABLE_REASON in reason:
        return "deny-unparseable"
    return "deny-gate"


def _run_cases(title, cases, check):
    print(title)
    failures = 0
    for case in cases:
        ok, label, text = check(case)
        failures += 0 if ok else 1
        print(f"{'PASS' if ok else 'FAIL'} [{label}] {text}")
    print(f"\n{len(cases) - failures}/{len(cases)} OK")
    return failures


def _check_verb(case):
    command, expect_blocked = case
    blocked = False
    for segment in _segments(command):
        p = _parse_git(segment)
        if p is not None and not _verb_allowed(p[1], p[2]):
            blocked = True
            break
    return (blocked == expect_blocked,
            "block" if expect_blocked else "pass ", command)


def _check_parse(case):
    command, verb, gitdirs, worktrees = case
    parsed = _parse_git(command)
    got = None if parsed is None else (parsed[1], parsed[3], parsed[4])
    return got == (verb, gitdirs, worktrees), "parse", f"{command}  -> {got}"


def _check_repo(case):
    command, cwd, expected = case
    hit = evaluate(command, cwd)
    scope = f" [{hit['scope']}]" if hit else ""
    return ((hit is not None) == expected,
            "block" if expected else "pass ",
            f"(cwd={cwd or '<leer>'}) {command}{scope}")


def _check_cwd(case):
    data, expected = case
    got = _cwd_of(data)
    return got == expected, "cwd  ", f"{data} -> {got!r} (erwartet {expected!r})"


def _check_e2e(case):
    label, payload, expected = case
    try:
        rc, out = _run_gate(payload)
    except Exception as exc:
        return False, "e2e  ", f"{label}: Subprozess fehlgeschlagen ({exc})"
    got = _classify_gate_output(rc, out)
    return got == expected, "e2e  ", f"{label}  -> {got} (erwartet: {expected})"


def selftest():
    failures = _run_cases("== Verb-/Form-Klassifikation (ohne Repo) ==", CASES, _check_verb)
    failures += _run_cases("\n== Praefix-/Wert-Extraktion (verb, gitdirs, worktrees) ==",
                           PARSE_CASES, _check_parse)
    failures += _run_cases(
        "\n== Mit Repo-Scope (liest den echten ~/.claude-Checkout, mutiert nichts) ==",
        REPO_CASES, _check_repo)
    failures += _run_cases("\n== cwd-Shape aus dem Hook-Payload ==",
                           CWD_CASES, _check_cwd)
    failures += _run_cases(
        "\n== JSON-Shape + cwd end-to-end (main() ueber echten stdin) ==",
        E2E_CASES, _check_e2e)

    print("\nBekannte Luecken (Layer, kein Zaun — bewusst nicht abgedeckt):")
    for gap, gap_cwd in KNOWN_GAPS:
        state = "erkannt" if evaluate(gap, gap_cwd) else "nicht erkannt"
        print(f"  GAP  (cwd={gap_cwd or '<leer>'}) {gap}  -> {state}")
    total = (len(CASES) + len(PARSE_CASES) + len(REPO_CASES)
             + len(CWD_CASES) + len(E2E_CASES))
    print(f"\nGesamt: {total - failures}/{total} OK")
    sys.exit(1 if failures else 0)


SCOPE_TEXT = {
    "cwd": "im HAUPT-Checkout von ~/.claude",
    "git-dir": "mit GIT_DIR/--git-dir auf den HAUPT-Checkout von ~/.claude",
    "work-tree": "mit einem nach ~/.claude umgelenkten Arbeitsbaum",
    "git-dir-cwd": ("mit einem FREMDEN --git-dir/GIT_DIR, dessen Arbeitsbaum "
                    "mangels --work-tree der cwd im Bereich von ~/.claude ist"),
}


UNPARSEABLE_REASON = (
    "[Merge-Gate] Der Hook hat kein auswertbares `tool_input.command` bekommen "
    "(fehlender oder nicht-stringartiger Wert). Ein nicht interpretierbares "
    "Kommando gilt als UNBEKANNT und wird geblockt statt durchgelassen — ohne "
    "diese Pruefung wuerde der Hook an dieser Stelle mit einer Exception "
    "sterben, und ein abgestuerzter PreToolUse-Hook laesst das Tool laufen "
    "(fail-open).")


# Eigener Text statt UNPARSEABLE_REASON, damit im Log unterscheidbar
# bleibt, WAS unauswertbar war — Kommando oder Scope.
UNPARSEABLE_CWD_REASON = (
    "[Merge-Gate] Der Hook hat ein `cwd` bekommen, das kein String ist. Der cwd "
    "bestimmt den SCOPE (liegt das Ziel im HAUPT-Checkout von ~/.claude?) und "
    "wird an os.path.expanduser/os.path.join weitergereicht. Ein nicht-"
    "stringartiger Wert laesst die Pruefung je nach Kommandoform mit einem "
    "TypeError sterben oder still am Scope vorbeilaufen — beides ist fail-open. "
    "Ein nicht bestimmbarer Scope gilt deshalb als UNBEKANNT und wird geblockt.")


def _command_of(data):
    """Der Kommandotext, "" wenn nichts zu pruefen ist, None = unauswertbar."""
    tool_input = data.get("tool_input") or {}
    if not isinstance(tool_input, dict):
        return None
    command = tool_input.get("command")
    if command is None or command == "":
        return ""
    if not isinstance(command, str):
        return None
    return command


def _cwd_of(data):
    """Der cwd-Text, "" wenn keiner mitgegeben wurde, None = unauswertbar."""
    cwd = data.get("cwd")
    if cwd is None or cwd == "":
        return ""
    if not isinstance(cwd, str):
        return None
    return cwd


def _deny_unparseable(*, reason=UNPARSEABLE_REASON):
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason}}))


# Entscheidungs-Protokoll (metrics/gates). Fail-open und bewusst defensiv
# importiert: ein fehlender oder kaputter Logger darf dieses Gate nie zum
# Absturz bringen -- ein abgestuerzter PreToolUse-Hook sperrt im schlimmsten
# Fall jeden Bash-Aufruf aus.
GATE_LOG_NAME = "merge-gate"
# Der Selftest darf die Messung nicht verschmutzen -- er faehrt praeparierte
# Payloads durch dieselbe Datei, teils als Subprozess (erbt das Env).
if "--selftest" in sys.argv:
    os.environ["CLAUDE_GATE_LOG_DISABLE"] = "1"
sys.path.append(os.path.join(os.path.dirname(os.path.abspath(__file__)), "lib"))
try:
    from gate_log import log_decision
except Exception:
    def log_decision(*_args, **_kwargs):
        pass


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--selftest":
        selftest()
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if not isinstance(data, dict):
        log_decision(GATE_LOG_NAME, "deny", "unparseable-payload", data)
        _deny_unparseable()
        return
    command = _command_of(data)
    if command is None:
        log_decision(GATE_LOG_NAME, "deny", "unparseable-command", data)
        _deny_unparseable()
        return
    cwd = _cwd_of(data)
    if cwd is None:
        log_decision(GATE_LOG_NAME, "deny", "unparseable-cwd", data)
        _deny_unparseable(reason=UNPARSEABLE_CWD_REASON)
        return
    hit = evaluate(command, cwd)
    if not hit:
        log_decision(GATE_LOG_NAME, "allow", "no-hit", data)
        sys.exit(0)
    mode = data.get("permission_mode", "")
    decision = "ask" if mode in ("default", "plan") else "deny"
    if hit.get("reason") == "gh-branch-switch":
        base = ("[Merge-Gate] `gh pr checkout` (auch als Alias `gh co`/`gh pr co`) "
                "im ~/.claude-Hauptcheckout erkannt. Es schaltet den Arbeitsbaum "
                "auf den PR-Branch um — dieselbe Waesche wie `git checkout "
                "<branch>`, nur ueber gh. Fuer den Remote-Zustand ist das Kommando "
                "lesend, fuer den Arbeitsbaum nicht. ")
    elif hit.get("reason") == "revert-kein-anker":
        base = (f"[Merge-Gate] `git revert` {SCOPE_TEXT[hit['scope']]}, aber der "
                f"eingefrorene Anker-Ref `{ANCHOR_REF}` ist im Hauptcheckout nicht "
                "gesetzt. Die revert-Freigabe haengt an diesem Anker statt "
                "an HEAD (HEAD ist nach einer Ref-Verschiebung aus einem Worktree "
                "selbst die potenziell vergiftete Groesse). Ohne Anker gibt es "
                "keinen Bezugspunkt, und ein unbestimmbarer Bezugspunkt gilt hier "
                "als UNBEKANNT -> geblockt. Setzen darf ihn nur der Maintainer selbst, im "
                "eigenen Terminal, nach Sichtpruefung des Standes: "
                f"`git -C ~/.claude update-ref {ANCHOR_REF} (gepruefter-sha)`. "
                "NICHT automatisiert und NICHT aus lokalem refs/heads/main "
                "abgeleitet — das zoege den Anker nach einem Hijack auf den "
                "vergifteten Commit. ")
    elif hit.get("reason") == "revert-fremd":
        base = (f"[Merge-Gate] `git revert` {SCOPE_TEXT[hit['scope']]} mit einem Ziel, "
                f"das NICHT Ancestor des eingefrorenen Ankers `{ANCHOR_REF}` ist "
                "(oder nicht aufloesbar war). Erlaubt ist revert nur fuer Ziele aus "
                "der Anker-Historie — ein Commit aus einem Worktree ist ueber den "
                "geteilten Object-Store zwar per SHA erreichbar, sein Revert "
                "schreibt und committet aber fremden Inhalt auf main, vorbei am "
                "separaten Write-Gate. Ein legitimer eigener Commit, "
                "der NEUER ist als der Anker, faellt hier ebenfalls — dann muss "
                "der Maintainer den Anker zuerst von Hand nachziehen. "
                "Der sanktionierte Ausweg bleibt: `git checkout -- (pfad)`, "
                "`git restore -- (pfad)`. ")
    else:
        base = (f"[Merge-Gate] `git {hit['verb']}` {SCOPE_TEXT[hit['scope']]} erkannt. "
            "Ref-bewegende git-Kommandos sind dort geblockt, weil sie die Fassung "
            "aendern, die die Live-Hooks lesen — ein Merge einer Task-Branch traegt "
            "Worktree-Aenderungen an geschuetzten Dateien vorbei am Write-Gate "
            "in den scharfen Stand. Erlaubt bleiben lesende Kommandos, `git add`/"
            "`git commit`, `git fetch` sowie die sanktionierten Auswege "
            "`git checkout -- <pfad>`, `git restore -- <pfad>` und `git revert` — "
            "letztere aber nicht, wenn der Arbeitsbaum umgelenkt ist ODER der Index "
            "aus einem fremden --git-dir kommt, weil sie dann fremden Inhalt statt "
            "des eigenen Standes materialisieren. ")
    if decision == "deny":
        reason = base + ("Im Auto-Approve/bypass-Modus hart geblockt. Stattdessen: auf "
                         "einen eigenen Branch pushen, PR aufmachen, Merge-Klick beim Maintainer — "
                         "oder der Maintainer merged lokal von Hand.")
    else:
        reason = base + "Vor Bestaetigung pruefen: soll der Hauptcheckout wirklich bewegt werden?"
    log_decision(GATE_LOG_NAME, decision,
                 hit.get("reason") or hit.get("verb") or "ref-move", data)
    print(json.dumps({"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": decision,
        "permissionDecisionReason": reason}}))


if __name__ == "__main__":
    main()
