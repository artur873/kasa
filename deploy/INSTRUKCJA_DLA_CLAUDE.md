# Instalacja kasy na serwerze - instrukcja dla Claude

Pracujesz na serwerze użytkownika (terminal w przeglądarce). Zadanie: uruchomić na nim **kasę** - narzędzie
finansowe ODM Partners (repozytorium `https://github.com/artur873/kasa`), które 3× dziennie pobiera operacje z
banków (EnableBanking) i zapisuje je w bazie Supabase (projekt `odm`). Wszystko jest przygotowane w katalogu
`deploy/` repozytorium; `deploy/README.md` opisuje kroki dla Ubuntu/Debiana.

## Zasady
- **Sekretów nie wpisuj ani nie wypisuj na ekran.** Hasło bazy, `KASA_PSEUDONYM_SECRET`, Application ID
  EnableBanking, identyfikatory sesji i klucz `.pem` wpisuje/wgrywa użytkownik sam. Ty mówisz gdzie i jak, a
  potem sprawdzasz tylko, czy pola są wypełnione (np. `sudo grep -c '=$' /etc/kasa/kasa.env` - ile pustych).
- Nie zmieniaj nic w bazie poza tym, co robią polecenia kasy z tej instrukcji. Baza jest wspólna z pocztą i
  innymi narzędziami (schematy `glos`, `rdzen`, `surowe`, `meta`, `zadania`, tabele `ruch`, `przebiegi`).
- Przed każdym krokiem, który coś instaluje albo zmienia w systemie, powiedz użytkownikowi, co zrobisz.
- Jeśli coś się nie zgadza z instrukcją - zatrzymaj się i opisz, zamiast obchodzić.

## Krok 0: rozpoznanie serwera
Sprawdź i zapisz wyniki:
```bash
cat /etc/os-release
```
```bash
ps -p 1 -o comm=
```
(`systemd` = jest systemd)
```bash
sudo -n true && echo "sudo bez hasła" || echo "sudo z hasłem albo brak"
```
```bash
df -h / && free -h
```
Wyjście do sieci (każde powinno coś odpowiedzieć, nie „timeout”):
```bash
curl -sS -o /dev/null -w "%{http_code} enablebanking\n" https://api.enablebanking.com/
```
```bash
curl -sS -o /dev/null -w "%{http_code} nbp\n" https://api.nbp.pl/
```
```bash
curl -sS -o /dev/null -w "%{http_code} github\n" https://github.com/
```
```bash
timeout 5 bash -c '</dev/tcp/aws-0-eu-central-1.pooler.supabase.com/5432' && echo "supabase 5432 OK" || echo "supabase 5432 ZABLOKOWANY"
```
Jeśli port 5432 do Supabase jest zablokowany - zatrzymaj się i powiedz użytkownikowi (trzeba zmienić firewall
u dostawcy serwera); bez tego kasa nie zapisze danych.

## Krok 1: instalacja
Najpierw sprawdź, czy katalog `deploy/` jest już na gałęzi `master`:
```bash
curl -fsS -o /dev/null -w "%{http_code}\n" https://raw.githubusercontent.com/artur873/kasa/master/deploy/install.sh
```
`200` = jest, użyj `master`. `404` = zmiany nie są jeszcze scalone - użyj gałęzi `claude/bank-eb-poprawki`
(i powiedz użytkownikowi, że warto ją scalić na GitHubie).

**Ubuntu / Debian z systemd** - instalator robi wszystko (dla gałęzi roboczej podmień obie nazwy gałęzi):
```bash
curl -fsSL https://raw.githubusercontent.com/artur873/kasa/master/deploy/install.sh | sudo KASA_BRANCH=master bash
```

**Inny system** (np. AlmaLinux/Rocky/CentOS/Fedora - `dnf`, Amazon Linux - `yum`, Alpine - `apk`): przeczytaj
`deploy/install.sh` i wykonaj te same kroki ręcznie, podmieniając tylko instalację pakietów (potrzebne: `git`,
`curl`, `ca-certificates`, klient PostgreSQL - opcjonalnie). Układ musi zostać ten sam: użytkownik systemowy
`kasa` z katalogiem domowym `/var/lib/kasa`, kod w `/opt/kasa`, Python 3.13 w `/opt/kasa/.venv` przez `uv`
(`uv venv --python 3.13`, `uv pip install -e '.[web]'`), sekrety w `/etc/kasa/kasa.env` (z
`deploy/kasa.env.example`, `root:kasa`, `640`), jednostki z `deploy/systemd/` w `/etc/systemd/system/`.
Bez systemd: zamiast timera wpis cron użytkownika `kasa` o 6:47, 12:47 i 18:47 czasu polskiego, który ładuje
`/etc/kasa/kasa.env` (`set -a; . /etc/kasa/kasa.env; set +a`) i uruchamia `/opt/kasa/deploy/kasa-bank.sh`
z wyjściem dopisywanym do `/var/lib/kasa/kasa-bank.log`.

## Krok 2: sekrety (robi użytkownik)
Poproś użytkownika, żeby sam otworzył plik i uzupełnił pola według komentarzy:
```bash
sudo nano /etc/kasa/kasa.env
```
- `KASA_DATABASE_URL` - podmienić `HASLO` na hasło bazy z panelu Supabase (Project Settings → Database);
- `KASA_PSEUDONYM_SECRET` - ten sam co w środowisku „Spinacz - Finanse” w Claude Code;
- `KASA_EB_APP_ID` - Application ID z panelu EnableBanking (aplikacja „ODM Partners - wyciagi”);
- `KASA_EB_SESSIONS` - `mbank=<session_id> millennium=<session_id>` (identyfikatory są w zmiennych środowiska
  „Spinacz - Finanse” albo w n8n → Executions workflow z webhookiem `eb-callback-7d41c9a2`).

Klucz prywatny EnableBanking (plik `.pem`) użytkownik wgrywa na serwer, a Ty instalujesz go tak:
```bash
sudo install -o root -g kasa -m 640 /SCIEZKA/DO/klucza.pem /etc/kasa/enablebanking.pem
```
Potem usuń kopię z miejsca, do którego został wgrany. Sprawdź tylko, że nie ma pustych pól:
```bash
sudo grep -E '^[A-Z_]+=$' /etc/kasa/kasa.env || echo "brak pustych pól"
```

## Krok 3: pierwsze uruchomienie
Każde polecenie po kolei; po każdym sprawdź wynik.
```bash
sudo /opt/kasa/deploy/kasa run check
```
Oczekiwane: połączenie z bazą, 15 migracji zastosowanych, nic do zrobienia. Jeśli `check` chce stosować
migracje albo zgłasza inną bazę - zatrzymaj się.
```bash
sudo /opt/kasa/deploy/kasa run init-db
```
Tworzy firmy `odm` i `rentease` (migracje bez zmian).
```bash
sudo /opt/kasa/deploy/kasa run --bank mbank bank ingest --company odm --from 2025-11-01 --to "$(date +%F)" --provider enablebanking --fx nbp
```
```bash
sudo /opt/kasa/deploy/kasa run --bank millennium bank ingest --company odm --from 2025-11-01 --to "$(date +%F)" --provider enablebanking --fx nbp
```
Oczekiwane (stan z 6.11.2026): mBank ok. 4400 operacji od 11.2025, Millennium ok. 1700 od 08.2026; wynik
pobrania mBanku mówi o „znaku przywróconym” w ok. 1200 operacjach - to normalne. Kod wyjścia 1 z czerwoną
kontrolą MPP przed wgraniem wyciągu VAT też jest normalny.

Wyciąg MT940 z rachunku VAT mBanku: poproś użytkownika o plik (ma go od 6.11.2026), wgraj na serwer i:
```bash
sudo install -o kasa -m 600 /SCIEZKA/DO/wyciagu.txt /var/lib/kasa/vat_mbank.mt940
```
```bash
sudo /opt/kasa/deploy/kasa run bank import-mt940 /var/lib/kasa/vat_mbank.mt940 --company odm --kind vat
```
Oczekiwane: ok. 375 operacji na PL49 1140 2004 0000 3503 0090 4100, uwaga o odwróconych znakach sald (normalne).

Kontrola miesiąca:
```bash
sudo /opt/kasa/deploy/kasa run bank balance-check --company odm --month 2026-10
```
Oczekiwane: `[OK]` dla trzech rachunków (dwa bieżące i VAT) i `[OK] MPP` z kwotą 53881.15 po obu stronach.

## Krok 4: harmonogram
```bash
sudo systemctl start kasa-bank.timer
```
```bash
systemctl list-timers kasa-bank.timer
```
```bash
sudo systemctl start kasa-bank.service && journalctl -u kasa-bank.service -n 60 --no-pager
```
Oczekiwane: przebieg dla `mbank` i `millennium`, status zgody AUTHORIZED (ważna do 4.05.2027), kod wyjścia 0.

## Na koniec - raport dla użytkownika
Krótko: system i wersja, czy instalacja przeszła, wynik `check`, liczby operacji z pobrań, wynik importu VAT,
wynik `balance-check` za 2026-10, kiedy najbliższe uruchomienie timera, oraz wszystko, co poszło inaczej niż tu
opisano. Przypomnij: wyciąg VAT z mBanku wgrywa się co miesiąc tym samym poleceniem `import-mt940`
(opis w `/opt/kasa/deploy/README.md`).
