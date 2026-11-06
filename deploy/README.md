# Kasa na serwerze

Zestaw do uruchomienia kasy na własnym serwerze (Ubuntu/Debian z systemd): kod w `/opt/kasa`, Python 3.13 w
`/opt/kasa/.venv` (przez `uv`), sekrety w `/etc/kasa/kasa.env`, użytkownik systemowy `kasa`, baza w Supabase
(projekt **odm**), pobieranie z EnableBanking 3× dziennie (`kasa-bank.timer`).

Serwer musi mieć wyjście do: `aws-0-eu-central-1.pooler.supabase.com:5432` (Supabase), `api.enablebanking.com`,
`api.nbp.pl`, `github.com` (kod) i `astral.sh` / `github.com` (instalator `uv` i Pythona).

## 1. Instalacja

```bash
curl -fsSL https://raw.githubusercontent.com/artur873/kasa/master/deploy/install.sh | sudo bash
```

Skrypt można powtarzać - tak samo wygląda aktualizacja kodu po scaleniu zmian na `master`.

## 2. Sekrety

```bash
sudo nano /etc/kasa/kasa.env
```

Uzupełnij według komentarzy w pliku: hasło bazy Supabase, `KASA_PSEUDONYM_SECRET` (ten sam co dotąd),
`KASA_EB_APP_ID`, identyfikatory sesji EnableBanking (`KASA_EB_SESSIONS`). Klucz prywatny aplikacji EB:

```bash
sudo install -o root -g kasa -m 640 ~/sciezka/do/klucza.pem /etc/kasa/enablebanking.pem
```

## 3. Pierwsze uruchomienie

```bash
sudo /opt/kasa/deploy/kasa run check
```

Połączenie z Supabase i stan migracji (wszystkie 15 są już w bazie - nic nie zostanie zmienione).

```bash
sudo /opt/kasa/deploy/kasa run init-db
```

Firmy `odm` i `rentease` oraz słowa wrażliwe (migracje bez zmian).

```bash
sudo /opt/kasa/deploy/kasa run --bank mbank bank ingest --company odm --from 2025-11-01 --to 2026-11-06 --provider enablebanking --fx nbp
```

```bash
sudo /opt/kasa/deploy/kasa run --bank millennium bank ingest --company odm --from 2025-11-01 --to 2026-11-06 --provider enablebanking --fx nbp
```

Historia z obu banków (mBank oddaje ok. 12 miesięcy, Millennium ok. 3). Kod wyjścia 1 = pobrane, ale któraś
kontrola prawdy jest czerwona - przed wgraniem wyciągu VAT to normalne (MPP).

Wyciąg MT940 z rachunku VAT mBanku (skopiuj plik na serwer, np. do `/tmp`):

```bash
sudo install -o kasa -m 600 /tmp/vat_mbank.mt940 /var/lib/kasa/vat_mbank.mt940
```

```bash
sudo /opt/kasa/deploy/kasa run bank import-mt940 /var/lib/kasa/vat_mbank.mt940 --company odm --kind vat
```

Na koniec włącz harmonogram:

```bash
sudo systemctl start kasa-bank.timer
```

## 4. Na co dzień

* **Pobieranie** - samo, 3× dziennie (06:47, 12:47, 18:47 czasu polskiego), ostatnie 5 dni z każdego banku.
  Podgląd: `systemctl list-timers kasa-bank.timer`, dziennik: `journalctl -u kasa-bank.service -n 200`.
  Ręcznie: `sudo systemctl start kasa-bank.service`.
* **Wyciąg VAT z mBanku co miesiąc** - kopiujesz plik na serwer i uruchamiasz `bank import-mt940 ... --kind vat`
  jak wyżej (ten sam wyciąg wgrany drugi raz niczego nie dubluje).
* **Kontrola miesiąca**: `sudo /opt/kasa/deploy/kasa run bank balance-check --company odm --month 2026-11`.
* **Zgoda bankowa (180 dni, do 4.05.2027)** - skrypt pobierania sprawdza ją przy każdym przebiegu; na 14 dni przed
  końcem usługa kończy się błędem (`systemctl status kasa-bank.service`). Odnowienie: nowa autoryzacja w banku,
  nowy `session_id` z n8n → `KASA_EB_SESSIONS` w `/etc/kasa/kasa.env`.
* **Aktualizacja kodu** - po scaleniu zmian na `master` uruchom ponownie instalator (krok 1).

Kod wyjścia usługi: 0 - w porządku (czerwona kontrola prawdy jest tylko w dzienniku), 2 - pobranie się nie udało,
3 - zgoda kończy się w ciągu 14 dni.
