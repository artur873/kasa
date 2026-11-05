# Dashboard (faza 5)

Krok 8 briefingu: dashboard składa się z tych samych metryk co agent - **karta = „metryka@wersja + wymiary +
filtry”**. Każda karta pokazuje datę danych, wersję metryki, udział niepotwierdzonych kwot (w otwartych sugestiach)
i czy kontrole prawdy w tym okresie są zielone. Własnej formuły nie da się wpisać.

Kod: `kasa/web/routers/dashboard.py` (podpięty w `kasa/web/app.py` pod `/dashboard` i `/api/dashboard`), szablon
`kasa/web/templates/dashboard/index.html`, układ ODM `kasa/extensions/odm/dashboard.yaml`, testy
`tests/metrics/test_dashboard.py` i `test_e_dashboard_and_agent_give_the_same_numbers` w bramce 5.

## 1. Układ należy do rozszerzenia, rdzeń renderuje

`kasa/extensions/<firma>/dashboard.yaml`:

```yaml
title: ODM - liczby z warstwy metryk
cards:
  - metric: receivables_open@1        # klucz@wersja - obowiązkowo z wersją
    title: Należności otwarte i wiek
    chart: aging                      # number | table | aging (słupki kubełków) | bars
  - metric: margin_accrual@3
    title: Marża per event (memoriałowo, cały event)
    chart: bars
    bar_field: margin_minor           # które pole wiersza metryki rysować
    label_field: cost_object
  - metric: costs_without_document@1
    title: Koszty bez dokumentu (> 100 zł)
    chart: table
    # dimensions: [...], filters: {...} - tylko te, które definiuje metryka
    # period: data_month (domyślnie) | previous_month | RRRR-MM
```

Model karty (`CardSpec`, pydantic `extra="forbid"`) ma tylko pola: `metric`, `title`, `dimensions`, `filters`,
`period`, `chart`, `bar_field`, `label_field`. Klucz spoza listy (`formula`, `sql`, `expression`…) = błąd układu;
metryka bez wersji = błąd; nieznany wymiar albo filtr odrzuca rejestr metryk. Żaden parametr żądania nie definiuje
liczby (jedyny parametr: `?month=RRRR-MM`). Bez pliku układu dashboard pokazuje wszystkie metryki w bieżących
wersjach.

## 2. Karta

Wartość, wiersze i słupki pochodzą z `kasa.core.metrics.registry.compute` - tego samego wywołania, którego używa
narzędzie MCP `get_metric`. Na karcie: tytuł, `metric@wersja`, „dane na RRRR-MM-DD” (dzień stanu danych), okres,
„w sugestiach: X%” (udział niepotwierdzonych, pomarańczowy gdy > 0), „kontrole prawdy: zielone / czerwone (n) /
nie liczone”, liczba główna, wykres SVG inline (słupki wiekowania należności i zobowiązań, marża i cash per obiekt,
pokrycie alokacji; ujemne na czerwono od osi środkowej), tabela wierszy, do trzech ostrzeżeń metryki. Pod kartami:
otwarte propozycje metryk od agenta (pytania spoza warstwy) i szkice przypomnień o płatności (wysyłasz Ty).

Mobile-first (szerokość jak kolejka), bez frameworków i bez JavaScriptu; nawigacja „Liczby” w nagłówku panelu.

`GET /api/dashboard[?month=RRRR-MM]` - te same karty jako JSON (pełny wynik metryki w `result`).

## 3. Uruchomienie

```bash
python -m kasa web --host 0.0.0.0 --port 8787 --user artur     # jak kolejka (docs/KOLEJKA.md, rozdz. 8)
# telefon: http://<adres>:8787/dashboard?token=…
```

Każde żądanie dashboardu synchronizuje definicje metryk (`metrics sync` bez przebudowy luster); definicja zmieniona
bez nowej wersji nie wywraca strony - jej karta pokazuje odmowę rejestru.

## 4. Decyzje własne [5]

* Dashboard czyta na roli właściciela (panel pokazuje dane jawne - jak kolejka), agent na `kasa_agent_ro`; metryki
  pokazują osoby tylko kodem w obu ścieżkach, więc liczby i wiersze są identyczne (test).
* Karta okresowa domyślnie bierze miesiąc stanu danych; metryki „na dzień” - dzień stanu danych.
* Wykresy: proste słupki poziome z podpisem i kwotą - bez osi i siatek (na telefonie kwota obok słupka czyta się
  lepiej niż skala).
