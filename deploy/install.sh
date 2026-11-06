#!/usr/bin/env bash
# Instalacja kasy na serwerze (Ubuntu/Debian z systemd). Uruchom jako root:
#   curl -fsSL https://raw.githubusercontent.com/artur873/kasa/master/deploy/install.sh | sudo bash
# albo z klonu repo: sudo bash deploy/install.sh
# Skrypt można uruchamiać wielokrotnie: aktualizuje kod i zależności, nie rusza /etc/kasa/kasa.env.
set -euo pipefail

REPO="${KASA_REPO:-https://github.com/artur873/kasa.git}"
BRANCH="${KASA_BRANCH:-master}"
KASA_DIR=/opt/kasa

[[ $EUID -eq 0 ]] || { echo "Uruchom jako root (sudo)." >&2; exit 1; }
command -v systemctl >/dev/null || { echo "Brak systemd - ten skrypt jest dla Ubuntu/Debiana." >&2; exit 1; }

echo "== Pakiety systemowe"
apt-get update -qq
apt-get install -y -qq git curl ca-certificates postgresql-client >/dev/null

echo "== Użytkownik i katalogi"
id kasa >/dev/null 2>&1 || useradd --system --create-home --home-dir /var/lib/kasa --shell /usr/sbin/nologin kasa
install -d -o kasa -g kasa -m 700 /var/lib/kasa
install -d -o root -g kasa -m 750 /etc/kasa

echo "== Kod ($REPO, gałąź $BRANCH)"
if [[ -d $KASA_DIR/.git ]]; then
    git -C "$KASA_DIR" fetch -q origin "$BRANCH"
    git -C "$KASA_DIR" checkout -q "$BRANCH"
    git -C "$KASA_DIR" reset -q --hard "origin/$BRANCH"
else
    git clone -q --branch "$BRANCH" "$REPO" "$KASA_DIR"
fi
chown -R kasa:kasa "$KASA_DIR"

echo "== Python 3.13 i zależności (uv)"
if ! command -v uv >/dev/null; then
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin INSTALLER_NO_MODIFY_PATH=1 sh >/dev/null
fi
sudo -u kasa -H bash -c "cd $KASA_DIR && UV_PYTHON_INSTALL_DIR=/var/lib/kasa/python uv venv -q --python 3.13 .venv \
    && uv pip install -q --python .venv/bin/python -e '.[web]'"

echo "== Sekrety"
if [[ ! -f /etc/kasa/kasa.env ]]; then
    install -o root -g kasa -m 640 "$KASA_DIR/deploy/kasa.env.example" /etc/kasa/kasa.env
    echo "   Utworzono /etc/kasa/kasa.env z szablonu - UZUPEŁNIJ go przed pierwszym uruchomieniem."
fi
chmod 640 /etc/kasa/kasa.env; chown root:kasa /etc/kasa/kasa.env

echo "== Harmonogram (systemd)"
install -m 644 "$KASA_DIR/deploy/systemd/kasa-bank.service" /etc/systemd/system/
install -m 644 "$KASA_DIR/deploy/systemd/kasa-bank.timer" /etc/systemd/system/
systemctl daemon-reload
systemctl enable -q kasa-bank.timer

cat <<MSG

Gotowe. Dalej (szczegóły: $KASA_DIR/deploy/README.md):
  1. uzupełnij /etc/kasa/kasa.env i wgraj klucz EB do /etc/kasa/enablebanking.pem
  2. sudo $KASA_DIR/deploy/kasa run check           # połączenie z Supabase i stan bazy
  3. sudo $KASA_DIR/deploy/kasa run init-db         # firmy (migracje już są w bazie)
  4. pierwsze pobranie historii i wyciąg VAT - README, krok „Pierwsze uruchomienie”
  5. sudo systemctl start kasa-bank.timer
MSG
