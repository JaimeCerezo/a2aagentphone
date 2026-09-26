#!/usr/bin/env bash
#
# a2aagentphone — update every phone on this machine, and check nobody edited it.
#
# The cheap path. It takes no arguments because it needs none: the phones are
# already described in /etc/a2aagentphone/*.env, so there is nothing for an agent to
# decide, look up or reason about. One command, a few lines of output.
#
#   curl -fsSLO https://raw.githubusercontent.com/JaimeCerezo/a2aagentphone/main/scripts/update.sh
#   sudo bash update.sh
#
# Add --check to look without changing anything: it prints the installed
# version, whether it is current, and whether any installed file has been
# modified. Costs nothing and touches nothing.
#
set -euo pipefail

VERSION="v0.3.7"
REPO="https://github.com/JaimeCerezo/a2aagentphone"
VENV=/opt/a2aagentphone/venv
ETC=/etc/a2aagentphone
UNIT=/etc/systemd/system/a2aagentphone@.service

# Una salida cortada se leía como éxito. Este script imprime su progreso línea
# a línea, y la última que llegó a imprimirse el 2026-09-22 fue "updated to
# 0.3.5" -- que suena a final feliz y era el paso de en medio: el paquete ya
# estaba en disco y el listener seguía siendo el viejo. Quien mira la salida no
# tiene por qué saber qué línea era la última prevista, así que lo decimos.
trap 'rc=$?; [ "$rc" -eq 0 ] || echo "  SIN TERMINAR -- el script murió con código $rc; el listener puede seguir siendo el viejo"' EXIT


# Which version to install. The constant below is a floor, not the answer:
# raw.githubusercontent serves a cached copy of this script for a few minutes
# after a release, so whoever downloads it right after a tag lands gets the
# PREVIOUS script -- carrying the previous version number, and silently
# installing software older than the one they asked for. It bit twice on
# 2026-09-22, once badly enough to mint a token that broke a live line.
#
# So the script stops being the source of that answer. `git ls-remote` asks the
# repository directly, which no CDN sits in front of, and the newest tag wins.
# A copy of this script from any date installs the current release.
resolve_version() {
    local latest
    latest=$(git ls-remote --tags --refs "$REPO" 2>/dev/null \
             | sed 's#.*/##' | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' \
             | sort -V | tail -1)
    if [ -n "$latest" ]; then
        [ "$latest" != "$VERSION" ] && echo "  latest release is $latest (this script shipped with $VERSION)"
        VERSION="$latest"
    fi
}

CHECK=no
[ "${1:-}" = "--check" ] && CHECK=yes

die() { echo "a2aagentphone: $*" >&2; exit 1; }

[ -x "$VENV/bin/python" ] || die "no install at $VENV. Use install.sh first."
[ "$CHECK" = yes ] || [ "$(id -u)" -eq 0 ] || die "run me with sudo (or --check)."

# -P on every python call here, and it is not decoration. Without it Python
# puts the current directory on sys.path, so running this from a checkout of
# a2aagentphone imports the CHECKOUT instead of the install: the version came from
# the wrong tree and the integrity check below verified the source against
# itself and passed. An integrity check you can fool by cd'ing somewhere is
# worse than none, because it reports "clean".
resolve_version
have=$("$VENV/bin/python" -P -c 'import a2aagentphone;print(a2aagentphone.__version__)' 2>/dev/null || echo "?")
echo "a2aagentphone: installed $have, current ${VERSION#v}"

# --- has anyone edited the code? ------------------------------------------
# The rule is that a phone is not modified locally: it is one implementation,
# updated from one place, so that what a caller reaches is the same thing
# everywhere and an update never has to be merged with somebody's local fix.
#
# A rule nobody can check is a wish, so this checks it. Every wheel ships a
# RECORD with a hash per file; comparing against it catches an edited module,
# and an extra file under the package catches a local "improvement" bolted on
# beside it. Neither is prevented -- the operator has root -- but neither can
# happen quietly, which is the part that matters.
site=$("$VENV/bin/python" -P -c 'import sysconfig;print(sysconfig.get_paths()["purelib"])')
record=$(ls -d "$site"/a2aagentphone-*.dist-info/RECORD 2>/dev/null | head -1 || true)
drift=0
if [ -n "$record" ]; then
    while IFS=, read -r path hash _; do
        case "$path" in a2aagentphone/*.py) ;; *) continue ;; esac
        [ -f "$site/$path" ] || { echo "  MISSING  $path"; drift=1; continue; }
        want=${hash#sha256=}
        got=$("$VENV/bin/python" -P - "$site/$path" <<'PY'
import base64,hashlib,sys
d=hashlib.sha256(open(sys.argv[1],'rb').read()).digest()
print(base64.urlsafe_b64encode(d).rstrip(b'=').decode())
PY
)
        [ "$got" = "$want" ] || { echo "  MODIFIED $path"; drift=1; }
    done < "$record"

    known=$(awk -F, '{print $1}' "$record" | grep '^a2aagentphone/' || true)
    while read -r f; do
        rel=${f#"$site/"}
        echo "$known" | grep -qxF "$rel" || { echo "  EXTRA    $rel"; drift=1; }
    done < <(find "$site/a2aagentphone" -name '*.py' 2>/dev/null)
fi

if [ "$drift" = 1 ]; then
    echo
    echo "  This install has been changed locally. a2aagentphone is a closed product:"
    echo "  the next update overwrites this, and a caller can no longer tell what"
    echo "  it reached. Leave the finding in the mailbox instead --"
    echo "    a2aagentphone-note \"...\"   (see GOVERNANCE.md)"
    echo "  Re-running this script without --check restores the published files."
else
    echo "  files match the published release"
fi

# Surfaced here because this is the command that gets run on every machine, and
# a mailbox nobody empties is the same as no mailbox.
MAILBOX=/var/lib/a2aagentphone/mailbox.md
if [ -f "$MAILBOX" ]; then
    notes=$(grep -c '^## ' "$MAILBOX" 2>/dev/null) || notes=0
    [ "${notes:-0}" -gt 0 ] && echo "  mailbox: $notes note(s) waiting -- a2aagentphone-note --read"
fi

if [ "$CHECK" = yes ]; then
    for env in "$ETC"/*.env; do
        [ -f "$env" ] || continue
        n=$(basename "$env" .env)
        printf '  phone %-16s %s\n' "$n" "$(systemctl is-active "a2aagentphone@$n" 2>/dev/null || echo unknown)"
    done
    exit $drift
fi

# --- update ---------------------------------------------------------------
# The package is only replaced when it needs replacing. Everything BELOW this
# runs every single time, and that is the point: v0.1.24 returned early when
# the version already matched, which is exactly the machine that needs the rest
# -- one that took an update months ago and has been drifting since. "Already
# current" was answering a question nobody asked.
if [ "$have" = "${VERSION#v}" ] && [ "$drift" = 0 ]; then
    echo "  package already current"
else
    "$VENV/bin/pip" install --quiet --upgrade --force-reinstall "git+$REPO@$VERSION" \
        || die "update failed; the running phone is untouched."
    now=$("$VENV/bin/python" -P -c 'import a2aagentphone;print(a2aagentphone.__version__)')
    echo "  package updated to $now -- listener still the old one until restarted"
    package_changed=yes
fi

changed=$("$VENV/bin/python" -P -m a2aagentphone.deploy converge 2>/dev/null) || true
[ -n "$changed" ] && { echo "$changed"; deployment_changed=yes; }

# systemd no relee un fichero de unidad porque haya cambiado en disco: hay que
# decírselo. `install.sh` lo hacía y esto no, así que una máquina que tomaba el
# camino barato --el que le decimos a todo el mundo que tome-- arrancaba el
# ExecStart VIEJO contra el binario NUEVO.
#
# Medido por scm-intranet el 2026-09-22 actualizando a 0.3.0: systemd lanzó la
# unidad anterior, con --auth-token-file y --token-expires, contra un binario
# que ya no los acepta. status=2/INVALIDARGUMENT y 23 segundos sin teléfono
# (17:47:40 → 17:48:03).
#
# Es la misma familia que todo lo demás de este módulo, y la más cara de todas:
# lo que el instalador hacía y el actualizador no, falla en cada máquina ya
# instalada y sólo en la ruta que más se usa.
systemctl daemon-reload

# La pregunta que salva no es "¿he cambiado algo en ESTA ejecución?" sino "¿lo
# que corre es lo que hay en disco?". Con la primera, un update que muere entre
# el pip y el restart no se arregla nunca: al reintentar, el paquete ya está al
# día, converge no imprime nada, y el script decía "nothing to restart" sobre
# una máquina con código nuevo en disco y proceso viejo sirviendo. Se quedaba
# así indefinidamente, y sólo una mano lo sacaba de ahí.
#
# Con la segunda, un update fallido se arregla solo en la pasada siguiente.
# Cualquiera de las tres cosas que definen al teléfono -- el paquete, su unidad
# y su env -- más nueva que el arranque del servicio significa que lo que corre
# ya no es lo que hay en disco. Un servicio que nunca arrancó también cuenta.
stale() {
    local n=$1 env=$2 started
    started=$(systemctl show -p ActiveEnterTimestamp --value "a2aagentphone@$n" 2>/dev/null) || return 0
    [ -n "$started" ] || return 0
    started=$(date -d "$started" +%s 2>/dev/null) || return 0
    [ -n "$(find "$site/a2aagentphone" "$UNIT" "$env" -newermt "@$started" -print -quit 2>/dev/null)" ]
}

forced=no
[ -n "${package_changed:-}${deployment_changed:-}" ] && forced=yes
restarts=0

for env in "$ETC"/*.env; do
    [ -f "$env" ] || continue
    n=$(basename "$env" .env)
    systemctl is-enabled "a2aagentphone@$n" >/dev/null 2>&1 || continue

    if [ "$forced" = no ] && ! stale "$n" "$env"; then
        continue
    fi
    restarts=$((restarts + 1))

    # Is ANYBODY on this phone right now? An agent updating itself while
    # answering a call is the normal case, not an edge one -- it is how every
    # machine is meant to keep current -- and restarting the service that is
    # serving the call kills the call, the answer, and this script, halfway.
    # So when the phone is busy we let go of the restart: hand it to systemd,
    # which is not about to die, and let it happen once the call has hung up.
    #
    # NOT `grep a2aagentphone@$n.service /proc/self/cgroup`. That asked "am I
    # inside?" -- identity by location -- and scm-intranet measured it failing
    # on 2026-09-22 at 19:03:50 while updating to 0.3.4: the answering agent
    # had launched this script with `systemd-run --unit=a2a-update` so the
    # update would survive being cut, which is exactly what every call's
    # preamble asks for. Detached, the script saw itself in a2a-update.service,
    # concluded it was in no phone at all, and took the bare restart below.
    # 0.3.4 landed at 19:04:29; the call died at 19:04:30 with code 143,
    # unacknowledged. Any reasonable way of detaching -- systemd-run, nohup,
    # setsid, tmux -- breaks a question about where the asker runs.
    #
    # The condition that matters is about the phone, not about us, and the
    # waiter below already knows how to read it: anything in the unit's cgroup
    # other than its MainPID is a call in progress. An empty ControlGroup or a
    # MainPID of 0 means the phone is not running at all -- do not read the
    # root cgroup and mistake the whole machine for a caller.
    cg="/sys/fs/cgroup$(systemctl show -p ControlGroup --value "a2aagentphone@$n")/cgroup.procs"
    main=$(systemctl show -p MainPID --value "a2aagentphone@$n")
    busy=0
    if [ -n "$main" ] && [ "$main" != "0" ] && [ -r "$cg" ]; then
        # El `|| true` no es cosmético. `grep` devuelve 1 cuando no encuentra
        # nada, y "no encuentra nada" es justo el caso bueno: teléfono ocioso,
        # en el cgroup sólo está el MainPID. Con `pipefail` el status de la
        # sustitución es el del grep, no el del wc, así que `set -e` mataba el
        # script AQUÍ -- una línea antes del restart.
        #
        # Medido por scm-intranet el 2026-09-22 actualizando a 0.3.5: paquete
        # 0.3.5 en disco, listener todavía 0.3.4, salida cortada y código 1.
        # Falla SÓLO cuando no hay nadie al teléfono, que es el caso que
        # creíamos seguro; con una llamada abierta grep encuentra líneas,
        # devuelve 0 y todo parece funcionar. Por eso pasó la prueba en vivo.
        busy=$( { grep -vx "$main" "$cg" 2>/dev/null || true; } | wc -l )
    fi

    # An operator on SSH waits too, and that is the price: the phone is busy,
    # and we cannot tell a caller's `claude` from anyone else's. The 20 min cap
    # and the RESTART IT YOURSELF line below are what that costs.
    if [ "$busy" -gt 0 ]; then
        # NOT a timer. v0.1.22 used `systemd-run --on-active=20` here and
        # scm-intranet took it apart the same day: a fixed delay does not wait
        # for anybody to hang up. It turned "you are killed at 0s" into "you
        # are killed at 35s" -- 35 rather than 20, because a systemd timer's
        # default AccuracySec is a minute. The call still died with SIGTERM.
        #
        # So the waiter watches the phone's own cgroup and restarts when the
        # last `claude` under it is gone, which is the actual condition. It
        # runs as its own transient unit, outside that cgroup, so it does not
        # see itself and does not die with the call.
        systemd-run --collect --unit="a2aagentphone-bounce-$n" \
            /bin/bash -c "
                for _ in \$(seq 1 600); do
                    busy=\$(grep -vx '$main' '$cg' 2>/dev/null | wc -l)
                    [ \"\$busy\" -eq 0 ] && break
                    sleep 2
                done
                systemctl restart 'a2aagentphone@$n'" >/dev/null 2>&1 \
            && printf '  phone %-16s restart deferred until this call ends (20 min cap)\n' "$n" \
            || printf '  phone %-16s RESTART IT YOURSELF after hanging up: systemctl restart a2aagentphone@%s\n' "$n" "$n"
        continue
    fi

    systemctl restart "a2aagentphone@$n"
    host=$(grep -oP '^A2A_HOST=\K.*' "$env"); port=$(grep -oP '^A2A_PORT=\K.*' "$env")
    sleep 2
    v=$(curl -fsS --max-time 10 "http://$host:$port/.well-known/agent-card.json" 2>/dev/null \
        | grep -oP '"version"\s*:\s*"\K[^"]+' | head -1 || true)
    if [ -n "$v" ]; then
        printf '  phone %-16s answering, card says %s\n' "$n" "$v"
    else
        printf '  phone %-16s NOT ANSWERING -- journalctl -u a2aagentphone@%s\n' "$n" "$n"
    fi
done

[ "$restarts" -gt 0 ] || echo "  nothing to restart -- every phone is running what is on disk"
