#!/usr/bin/env bash
# Pobranie operacji z EnableBanking dla każdej sesji z KASA_EB_SESSIONS (ostatnie N dni, domyślnie 5),
# potem kontrola ważności zgód. Uruchamiane przez kasa-bank.timer (3× dziennie) albo ręcznie.
#
# Kod wyjścia: 0 - pobrania się udały (czerwona kontrola prawdy jest w logu, ale nie zatrzymuje harmonogramu:
# kontrola MPP czeka na comiesięczny wyciąg VAT wgrywany ręcznie); 2 - błąd pobrania; 3 - zgoda kończy się
# w ciągu 14 dni (trzeba odnowić autoryzację w banku).
set -uo pipefail

KASA_DIR="${KASA_DIR:-/opt/kasa}"
PY="$KASA_DIR/.venv/bin/python"
DAYS="${KASA_BANK_DAYS:-5}"
FROM="$(date -d "-$DAYS days" +%F)"
TO="$(date +%F)"

if [[ -z "${KASA_EB_SESSIONS:-}" ]]; then
    echo "Brak KASA_EB_SESSIONS w /etc/kasa/kasa.env" >&2
    exit 2
fi

status=0
for pair in $KASA_EB_SESSIONS; do
    bank="${pair%%=*}"
    session="${pair#*=}"
    echo "=== $bank: operacje $FROM - $TO"
    out="$(mktemp)"
    KASA_EB_SESSION_ID="$session" "$PY" -m kasa bank ingest --company odm --from "$FROM" --to "$TO" \
        --provider enablebanking --fx nbp 2>&1 | tee "$out"
    rc=${PIPESTATUS[0]}
    # 1 is both "saved, but a truth check is red" and an uncaught error - only the first ends with "Zapisano"
    saved=$(grep -c "^Zapisano (dostawca" "$out" || true)
    rm -f "$out"
    if [[ $rc -eq 1 && $saved -gt 0 ]]; then
        echo "--- $bank: pobrane, ale kontrola prawdy czerwona (szczegóły wyżej)"
    elif [[ $rc -ne 0 ]]; then
        echo "!!! $bank: pobranie nie powiodło się (kod $rc)" >&2
        status=2
    fi
    KASA_EB_SESSION_ID="$session" "$PY" -m kasa bank consent --provider enablebanking
    if [[ $? -ne 0 && $status -eq 0 ]]; then
        echo "!!! $bank: zgoda kończy się w ciągu 14 dni - odnów autoryzację w banku" >&2
        status=3
    fi
done
exit $status
