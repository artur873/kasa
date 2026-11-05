# Kasa - briefing: jak działa segment finansowy Spinacza 2.0

Nov 3, 2026 · @Artur

## Po co to budujemy

Kasa czyta każdą fakturę i każdy przelew ODM dokładnie raz, zapisuje z nich wszystko, łączy przelewy z fakturami, przypisuje przychody i koszty do eventów i wystawia jedną warstwę liczb, z której czytają agent AI, dashboard i Ty w kolejce przeglądu. Na końcu dostajesz: należności z wiekiem, cash, marżę per event, koszty bez dokumentu i eksport dla biura - z dowodem przy każdej liczbie. To narzędzie zarządcze, nie księgowe: biuro dalej prowadzi księgi, my dajemy mu czyste dane.

&#91;embedded content: przepływ danych w kasie: 4 źródła, 4 półki, 1 pętla decyzji\]

Czytaj z góry na dół: źródła wchodzą raz, kod i model zapisują fakty, dopasowanie i alokacja proponują, Ty decydujesz w kolejce, a Twoje decyzje stają się regułami i wracają do dopasowania. Agent i dashboard czytają tylko warstwę metryk.

## Słowniczek - 16 pojęć, które wystarczą do zrozumienia reszty

Czytaj od góry; każde pojęcie używa tylko tych nad nim.

| Pojęcie | Co to znaczy po ludzku |
| --- | --- |
| Rekord | Jeden wiersz w bazie: jedna faktura, jedna pozycja faktury, jeden przelew. „Szeroki rekord” = zapisujemy z niego wszystko, co się da, także to, czego dziś nie użyjemy. |
| Rdzeń i rozszerzenie | Rdzeń = kod wspólny dla każdej firmy (faktura, przelew, dopasowanie). Rozszerzenie = plik konfiguracji jednej firmy: co jest u niej „projektem” (event, pokój), jak czytać jej arkusze, jej kategorie. ODM ma rozszerzenie, Rentease ma drugie (tylko na dane testowe). |
| KSeF | Krajowy System e-Faktur. Od 2026 każda krajowa faktura B2B istnieje tam jako plik XML ze wszystkimi polami. My ją tylko czytamy - nie wystawiamy. |
| FA(3) | Format tego XML (schemat z czerwca 2025). Mówi, które pola są obowiązkowe (numer, daty, NIP-y, kwota), a które opcjonalne (termin płatności, numer zamówienia). |
| JPK | Pliki, które biuro wysyła do urzędu (JPK\_V7 co miesiąc, JPK\_FA na żądanie). Używamy ich jako szkieletu historii sprzed KSeF. |
| EnableBanking (EB) | Pośrednik, przez którego legalnie (PSD2) pobieramy historię rachunków z mBanku i Millennium. Firma sama zakłada darmowe konto EB i podpina swoje rachunki. |
| MPP (split payment) | Przelew, w którym bank sam rozdziela kwotę: netto na rachunek bieżący, VAT na osobny rachunek VAT. Na wyciągu to dwie albo trzy operacje zamiast jednej. |
| Dopasowanie (match) | Powiązanie przelewu z fakturą: „ten wpływ 12 300 zł to zapłata za FV 7/10/2026”. Może być wiele faktur na jeden przelew i odwrotnie. |
| Obiekt kosztowy | To, do czego przypisujemy przychód i koszt, żeby policzyć marżę. W ODM to event, w Rentease pokój w miesiącu. Rdzeń nie wie, co to jest - mówi mu to rozszerzenie. |
| Alokacja | Przypisanie faktury, pozycji albo przelewu do obiektu kosztowego, w całości albo w udziałach (60% Gala, 40% Targi). |
| UNASSIGNED („nieprzypisane”) | Specjalny obiekt „koszt ogólny, nie dotyczy żadnego eventu”. To decyzja, nie brak decyzji - różnicę widać w raportach. |
| Sugestia vs decyzja | Sugestia = propozycja maszyny (model, reguła). Decyzja = Twoje kliknięcie albo automat działający na Twoją wcześniejszą zgodę. Leżą w osobnych tabelach; maszyna nigdy nie nadpisuje decyzji. |
| Reguła | Nauczony wzorzec z dowodem: „nadawca X to zawsze ZUS - 14 z 14 przypadków”. Powstaje z Twoich kliknięć, dojrzewa etapami, dopiero potem działa sama. |
| Kolejka przeglądu | Lista rzeczy, których system nie jest pewny, z maks. trzema propozycjami do kliknięcia. Główny ekran, na telefonie. |
| Warstwa metryk | Jedno miejsce, gdzie zdefiniowano „przychód”, „należności”, „marża”. Agent i dashboard czytają tę samą definicję - nie ma dwóch marż. |
| MCP / agent | Sposób, w jaki AI (Claude, Gemini) pyta bazę o liczby. Agent dostaje kilkanaście narzędzi typu „podaj metrykę”, „pokaż dokument”, „zaproponuj dopasowanie” - i żadnego „zatwierdź” ani „zapłać”. |
| Pseudonimizacja | Zamiana nazwiska, IBAN-u, telefonu na kod (P-7f3a) zanim tekst trafi do modelu AI. Klucz do odwrócenia zostaje tylko u nas. |

## Krok 1 - skąd biorą się dane: cztery źródła

Podłączamy cztery źródła i w tej kolejności: najpierw bank, bo bilans konta jest jedynym twardym testem, że niczego nie zgubiliśmy; potem KSeF; potem pliki z historią; na końcu PDF-y z maili, bo są najdroższe w odczycie i najmniej pewne.

| Źródło | Co daje | Jak podłączamy | Dlaczego tak, a nie inaczej |
| --- | --- | --- | --- |
| Bank (mBank, Millennium) | Każdą operację: data, kwota, nadawca, IBAN, tytuł, saldo. Pełna historia raz, potem przyrost 3× dziennie | ODM zakłada darmowe konto w EnableBanking, podpina rachunki (w tym rachunek VAT), zgoda ważna 180 dni, potem odnowienie w banku | Jeden kanał dla wszystkich polskich banków; zero CSV-ów klikanych ręcznie. Na zapas: plik camt.053 albo MT940 z bankowości, gdyby EB wyłączyło darmowy tryb (ryzyko przyjęte 2.11) |
| KSeF | Każdą fakturę sprzedaży i zakupu od 2026 jako XML z pełnymi pozycjami; plus historia, jeśli ODM ma coś w starym KSeF 1.0 | ODM wystawia w Aplikacji Podatnika certyfikat „tylko odczyt”, kasa pobiera paczki co 15 min | Jedno API zamiast integracji z Fakturownią, inFaktem itd. - działa u każdej polskiej firmy. Nigdy nie wystawiamy |
| Pliki z historią | Lata sprzed KSeF: JPK\_V7 (szkielet: kto, ile, kiedy), JPK\_FA (pozycje sprzedaży), eksport CSV z programu biura; plus pliki wewnętrzne: arkusze PM, listy płac | Kreator importu: wgrywasz plik, mapowanie kolumn zapisane raz w rozszerzeniu ODM | Bez API konkretnego programu. Arkusz PM to pełnoprawny dokument („rozliczenie wewnętrzne”), nie brak dokumentu - dzięki temu BLIK dla hostessy ma dowód |
| PDF z maili | To, czego nigdy nie będzie w KSeF: faktury zagraniczne (Google, Anthropic, Notion), paragony, rachunki od osób | Archiwum maili z projektu `glos` (95 tys. maili) już ma załączniki; model AI czyta PDF raz, z kontrolą, że pozycje sumują się do kwoty | Najdroższy kanał, dlatego ostatni. Ta sama faktura z PDF i z KSeF to jeden rekord (deduplikacja w kroku 2) |

Trzy zasady pozyskania: surowy plik (XML, JSON z banku, PDF) zapisujemy zawsze i nigdy nie kasujemy; każde pobranie można powtórzyć bez duplikatów; pierwszy import historii robimy raz, z szacunkiem kosztu przed startem.

## Krok 2 - co dokładnie zapisujemy z faktury i z przelewu

Z każdego dokumentu i przelewu zapisujemy wszystko, co niesie - także pola, których dziś nie użyjemy - bo pola, którego nie zapiszemy, nie da się dodać bez ponownego czytania całego archiwum. Każde pole ma trzy warstwy: co podał kod (fakt ze źródła), co dopisał model AI (raz, z wersją promptu), co zdecydował człowiek.

**Z faktury (KSeF, FA(3))** - przykład: FV 7/10/2026 dla klienta ACME, 12 300 zł brutto.

| Grupa | Pola | Po co nam to |
| --- | --- | --- |
| Tożsamość | numer KSeF (35 znaków, nadaje system), numer faktury w wersji surowej i znormalizowanej („FV 7/10/2026” → „FV7102026”), rodzaj (VAT, korekta, zaliczkowa, rozliczeniowa), skrót pliku, program wystawcy | Numer KSeF to najpewniejszy klucz, że dwa kanały pokazują tę samą fakturę. Numer znormalizowany pozwala znaleźć fakturę w tytule przelewu mimo innych spacji i zer |
| Daty | wystawienia, sprzedaży lub okres usługi (1-31.05), przyjęcia przez KSeF, termin płatności (jeśli podany) | Przychód liczymy po dacie sprzedaży, nie wpłaty. Termin płatności jest opcjonalny w FA(3) - jego brak nie znaczy „płatne od ręki” |
| Strony | NIP sprzedawcy i nabywcy, nazwy, rachunek do zapłaty, strony trzecie (faktor, płatnik) | Faktor albo płatnik zmieniają, kto faktycznie przeleje pieniądze |
| Kwoty | netto, VAT, brutto w walucie faktury, kurs, kwota w PLN, podsumowanie per stawka | Faktura w EUR trzyma EUR jako kwotę główną; PLN jest pochodną |
| Pozycje | nazwa, ilość, jednostka, cena, netto, stawka, brutto, dla korekty stan przed/po | Pozycja to miejsce, gdzie widać „Gala ACME 12.06 Kraków” - stąd rozpoznajemy event |
| Powiązania | faktura korygowana, zaliczka rozliczana, numer zamówienia, umowy | Korekta to osobny dokument spięty z pierwotnym; saldo liczymy z łańcucha, nigdy nie scalamy |
| Flagi | MPP (czy faktura każe płacić split paymentem), metoda kasowa, odwrotne obciążenie | MPP na fakturze i MPP w banku to dwa osobne fakty - faktura może mówić „nie”, a klient i tak zapłaci splitem |

**Z przelewu (EnableBanking)** - przykład: wpływ 12 300 zł od ACME z tytułem `/VAT/2300,00/IDC/5342650778/INV/FV 7/10/2026/TXT/gala`.

| Grupa | Pola | Po co nam to |
| --- | --- | --- |
| Tożsamość | stały identyfikator banku (entry\_reference), własny odcisk (konto + data + kwota + tytuł) na wypadek jego braku | Żeby to samo pobranie po tygodniu nie stworzyło duplikatu |
| Daty i kwoty | data księgowania, data waluty, kwota ze znakiem, waluta, kurs przy walutowych, saldo po operacji | Bilans: saldo otwarcia + wszystkie operacje = saldo zamknięcia, co do grosza |
| Nadawca | nazwa i IBAN tak, jak podał bank; osobno nasza hipoteza „kto to jest” z pewnością | Revolut jako nadawca nie jest klientem - klient jest w tytule. BLIK i karty nie mają IBAN-u i to jest normalne |
| Tytuł | tekst w całości plus to, co kod z niego wyłuskał: numery faktur, numer KSeF, blok MPP (kwota VAT, NIP, numer), okres („czynsz listopad”), telefon, referencja operatora | Tytuł jest najważniejszym sygnałem dopasowania; kod wyciąga identyfikatory, model dopisuje intencję („zapłata za fakturę”, „czynsz”, „zwrot”) i osoby/miejsca |
| Typ operacji | przelew, natychmiastowy, karta, BLIK, prowizja, podatek, ZUS, własny, VAT | Mechaniczny typ, nie kategoria - pomaga situ wiedzieć, czego szukać |
| Status końcowy (decyzja) | dopasowana / bez dokumentu z kategorią / transfer własny / do wyjaśnienia | Każdy zaksięgowany przelew kończy z dokładnie jednym statusem - to warunek testu bilansu |

Transakcje oczekujące (blokady kartą) trzymamy osobno: nie wchodzą do bilansu, pokazują tylko „ile naprawdę mam dostępne”.

**Deduplikacja** - ta sama faktura przychodzi z KSeF, z PDF w mailu i z JPK od biura. Łączymy obserwacje pod jednym rekordem, w kolejności pewności: numer KSeF → skrót pliku → (NIP, rodzaj, numer znormalizowany) przy zgodnej dacie i kwocie → reszta do kolejki. Ten sam numer u innego NIP-u to nigdy nie duplikat. Nic nie kasujemy - zostają trzy obserwacje i jeden dokument.

## Krok 3 - cztery półki bazy i dlaczego to najważniejsza zasada

Baza ma cztery półki i każdy program pisze tylko na swojej. Dzięki temu nowy przebieg modelu nigdy nie zniszczy Twojej pracy z kolejki, a każdą liczbę da się cofnąć do źródła.

| Półka | Co leży | Kto pisze | Czy można zmieniać |
| --- | --- | --- | --- |
| L0 - surowe | XML z KSeF, JSON z banku, PDF, wgrane pliki - dokładnie takie, jak przyszły, ze skrótem SHA-256 | pobieranie | Nigdy. Tylko dopisywanie. Z tej półki da się odtworzyć wszystko inne |
| L1 - fakty | Rekordy z kroku 2: faktury, pozycje, przelewy, kursy, odczyt modelu z tytułu | parser (kod) i model | Nowa wersja parsera lub promptu dopisuje nowy wiersz i oznacza stary jako zastąpiony. Stary zostaje |
| L2 - sugestie | Propozycje: „ten przelew to FV 7/10/2026 (p=0,97)”, „ta pozycja to event Gala”, „to kategoria IT/SaaS” | model, reguły, kod | Sugestia ma status: otwarta, przyjęta, odrzucona, wygasła. Nigdy nie udaje faktu |
| L3 - decyzje | Dopasowania, alokacje, kategorie, obiekty kosztowe, reguły - to, co obowiązuje | Ty w kolejce; automat tylko na Twoją wcześniejszą zgodę (reguła w statusie `auto`) | Decyzja człowieka może zostać zastąpiona tylko inną decyzją człowieka (pilnuje tego baza, nie prompt). Cofnięcie to nowa decyzja „cofam”, nie kasowanie |

Każdy wiersz na półkach L1-L3 niesie pochodzenie: skąd (źródło), kiedy, jaką metodą (kod, reguła, model, człowiek), z jaką pewnością, z jakim dowodem, w której wersji schematu i promptu. Agent, czytając liczbę, od razu wie, czy to Twoja decyzja, czy hipoteza modelu.

Dwie konsekwencje praktyczne: po roku pracy możesz zmienić model AI na inny, puścić go ponownie na całej historii i nic z Twoich kliknięć nie przepadnie; a gdy biuro zapyta „skąd ta marża”, odpowiedź to łańcuch: dokument → obserwacja z KSeF → dopasowanie z dowodem → Twoja decyzja z datą.

## Krok 4 - jak łączymy przelew z fakturą

Dopasowanie to kod działający na rekordach z kroku 2, warstwami od najtańszej i najpewniejszej do człowieka. Każda warstwa dopisuje sygnały i punkty do kandydata; żadna sama nie zamyka sprawy - zamyka automat (gdy spełni trzy warunki) albo Ty.

1. **Przygotowanie (kod).** Kwoty w groszach, numery faktur znormalizowane, nadawca po IBAN/NIP przypisany do kontrahenta, przelewy między własnymi kontami oznaczone jako „transfer własny”, dwie nogi split paymentu sklejone w jedno zdarzenie.
2. **Twarde identyfikatory.** Numer KSeF lub identyfikator zbiorczy w tytule (obowiązkowe w przelewach B2B od 1.01.2027), blok MPP z numerem i NIP-em, numer faktury w tytule. Kwota zgodna → automat. Kwota niezgodna → warstwa 3, ale sygnał zostaje.
3. **Kontrahent, kwota, czas.** Wiemy, kto przelał → bierzemy jego otwarte faktury → szukamy dokładnej kwoty w oknie od -30 do +90 dni wokół terminu. Tu też kombinacje: jeden przelew za 2-6 faktur (szukamy podzbioru sumującego się co do grosza), częściowa zapłata, nadpłata, korekta jako kwota ujemna, prowizja i kurs przy walutowych.
4. **Semantyka tytułu (model, już zapisana raz).** Gdy w tytule nie ma numeru: intencja („czynsz”, „zaliczka”, „zwrot”), osoba, obiekt, okres. Model nigdy nie widzi listy faktur, więc nie może sobie „dopasować” z głowy - opisuje tylko tekst.
5. **Reguły nauczone.** „Klient Z płaci zbiorczo raz w miesiącu”, „IBAN LT… + REV = klient ACME”. Każda z dowodem i statusem (krok 6).
6. **Ty.** Kolejka: przelew, maks. trzy propozycje z dowodem, jeden klik.

**Kiedy automat, a kiedy kolejka.** Automat wymaga naraz trzech rzeczy: skalibrowanej pewności co najmniej 99,5% (próg wynika z przyjętej proporcji: fałszywe dopasowanie kosztuje 200 razy więcej niż jeden klik), twardego dowodu (identyfikator, blok MPP, numer faktury przy zgodnym kontrahencie, reguła `auto` albo jedyna otwarta faktura kontrahenta na dokładnie tę kwotę) i wyraźnej przewagi nad drugim kandydatem. Brak któregokolwiek = kolejka. Dwie faktury po 4 500 zł i przelew 4 500 „zapłata” → zawsze Ty, bo remis.

**Co jest ważne przy łączeniu - pułapki, które schemat obsługuje z definicji:**

| Sytuacja | Co widać | Co robi kasa |
| --- | --- | --- |
| Split payment | Netto na bieżącym, VAT na rachunku VAT, do tego przeksięgowanie | Dwie nogi = jedno zdarzenie dopasowane do faktury na kwotę brutto; przeksięgowanie = transfer własny; bilans obu kont zamyka się |
| Jeden przelew za trzy faktury | Tytuł z trzema numerami, kwota = suma | Trzy dopasowania z kwotami, jedna grupa |
| Korekta zmniejszająca | Faktura 10 000, korekta -2 000, klient płaci 8 000 | Saldo liczone z łańcucha faktura + korekta; jeden przelew spina oba dokumenty |
| Revolut bez tytułu (Rentease) | Nadawca „Revolut”, 2 300 zł, pusty tytuł | Jedyny lokator z czynszem 2 300 w tym miesiącu → kolejka z jedną propozycją; po Twoim OK rodzi się reguła „ten IBAN = ten lokator” |
| Zagraniczny SaaS kartą | 437,12 zł kartą, faktura 99 EUR o dwa dni starsza | Dopasowanie z nazwaną różnicą „kurs”; faktura trzyma EUR, kurs z faktury albo NBP z dnia poprzedniego |
| Hostessa BLIK-iem | 450 zł na telefon, brak faktury, w arkuszu PM „Kowalska, 12.03, Allegro, 450” | Arkusz = dokument wewnętrzny; propozycja po kwocie, dacie i nazwisku → kolejka (Twoja decyzja 2.11: arkusz nie jest twardym dowodem, dopóki nie dojrzeje reguła) |
| Brak dokumentu w ogóle | ZUS, US, prowizja, zaliczka, kaucja, wydatek kartą czekający na fakturę | To osobna hipoteza z podtypami, nie „nie znaleźliśmy”. Zaliczka czeka 180 dni na fakturę; karta 30 dni |
| Nadpłata i duplikat | Druga identyczna wpłata | Nadpłata z flagą „możliwy duplikat”, zawsze kolejka; zwrot wiąże się z pierwotną wpłatą, nic nie znika |

Tolerancja kwoty (1% albo 20 zł, dla walut 1,5% albo 50 zł) działa tylko przy jednej fakturze na jeden przelew; przy wielu suma musi się zgadzać co do grosza. Każda różnica ma nazwę (prowizja, kurs, niedopłata) - nigdy nie „poprawiamy” kwoty.

## Krok 5 - eventy odkrywane z danych i jak koszt trafia do eventu

Lista eventów nie przychodzi z Airtable ani z arkusza (Twoja decyzja 2.11). Powstaje z danych: system grupuje pozycje faktur, sprawy z maili (oferty z datą i miejscem), przelewy i wiersze arkuszy PM w kandydatów na eventy, a Ty zatwierdzasz ich w karcie. Rdzeń nie wie, co to event - rozszerzenie ODM mówi: „event = klient + zwarte okno dat + miasto + nazwa”. Dla Rentease to samo mechanicznie daje „pokój × miesiąc”.

**Jak powstaje kandydat.** Potrzebna jest kotwica (sprawa z ofertą z datą i miejscem, pozycja faktury sprzedaży z nazwą własną, wiersz z importu listy) plus co najmniej jeden rekord innego typu, który model par uznał za „ten sam event” z wysoką pewnością. Model par uczy wag z Twoich decyzji (metoda Fellegi-Sunter, jak w narzędziach do łączenia rejestrów) - żadna waga nie jest wpisana na sztywno. Dwie oferty tego samego klienta z różnymi nazwami i datami nigdy nie sklejają się same; faktura zbiorcza nigdy nie jest kotwicą.

**Karta kandydata**: proponowana nazwa, klient, daty, miasto, lista rekordów (z typem), najsłabsze powiązanie, suma przychodu z ofert i faktur, suma kosztów. Przyciski: zatwierdź / podziel / połącz z / odrzuć / na pewno różne. Każde kliknięcie jest dowodem, który kalibruje model. Event nigdy nie powstaje, nie łączy się ani nie dzieli automatem.

**Jak koszt trafia do eventu** - sygnały od najpewniejszego:

| Siła | Sygnał | Przykład | Automat? |
| --- | --- | --- | --- |
| A | Jawne przypisanie w pliku wewnętrznym | Arkusz PM: „Kowalska, 12.03, Allegro, 450” | Tak |
| A | Jawny identyfikator w pozycji, tytule lub mailu | Numer zamówienia, nazwa eventu z unikalnym tokenem | Tak, gdy brak sprzeczności |
| B | Oferta ↔ faktura: zgodne pozycje i kwoty | Faktura 20 000 = oferta Gali 20 000 | Sugestia |
| B | Kontrahent + okres usługi | Hotel w Krakowie 11-15.06, klient ma jeden event w tym oknie | Sugestia; jeśli dwa eventy - kolejka |
| C | Podzbiór pozycji oferty, wspólni uczestnicy maili |  | Tylko podpowiedź |
| D | Sam klient, sama bliskość w czasie |  | Tylko zawężanie, nigdy przypisanie |
| - | Brak sygnału | Czynsz biura, SaaS | UNASSIGNED - koszt ogólny, nie zgadujemy |

Automat alokacji działa tylko dla sygnałów A albo gdy dojrzeje reguła kontrahenta (krok 6). Wszystko inne to sugestia w kolejce (Twoja decyzja 2.11).

**Faktura zbiorcza** (jedna pozycja „obsługa eventów maj”, 100 000 zł, a eventów było trzy) - kaskada: opis pozycji wskazuje event → pozycje zgadzają się z ofertami → podział proporcjonalnie do wartości ofert (20/30/50) → proporcjonalnie do osobodni z arkuszy → równo, ale tylko jeśli wcześniej zaakceptowałeś taką regułę dla tego klienta → UNASSIGNED. Jeśli dla jednego z trzech eventów brak oferty, pozycja idzie do kolejki z propozycją częściową (20/30 i reszta do uzupełnienia), nie dzieli się „na oko”. Korekta dziedziczy podział faktury pierwotnej, chyba że jej opis wskazuje konkretny event („rabat za Roadshow” → 100% Roadshow).

**Koszt jest jeden, dowodów wiele.** Przelew BLIK, wiersz arkusza PM i faktura PDF od tej samej osoby na tę samą kwotę i datę to jeden koszt - spięty, nie potrojony. Inaczej marża Allegro spadłaby trzy razy o 450 zł.

**Marża** liczona jest w dwóch ujęciach, które nigdy nie są jednym KPI: memoriałowo (przychód po dacie sprzedaży minus koszt przypisany, jak widzi biuro) i kasowo (wpływy minus wypływy, jak widzi bank). Łączy je „w drodze” = należności + zobowiązania. Raport firmy ma cztery wiersze: suma eventów, UNASSIGNED, niesparowane (jeszcze nie patrzyłeś), kontrola = zmiana salda; różnica kontrolna musi być 0,00 zł. ZUS i PIT od umów cywilnoprawnych w v1 idą do UNASSIGNED (Twoja decyzja), a arkusz PM od razu ma pole „typ rozliczenia” pod v2.

## Krok 6 - reguły, których system uczy się z Twoich kliknięć

Reguła to wzorzec z dowodem: „nadawca z tym IBAN-em → ZUS, 14 z 14 przypadków od stycznia”. Powstaje z danych i z Twoich decyzji, nigdy ze starych skilli. Jedno kliknięcie w kolejce to dowód; kilka zgodnych kliknięć to kandydat na regułę; reguła dojrzewa etapami, zanim zacznie działać sama.

| Etap | Co się dzieje | Kiedy przechodzi dalej |
| --- | --- | --- |
| Kandydat | System znalazł wzorzec (≥ 5 przypadków w ≥ 2 miesiącach) i pokazuje kartę: pytanie biznesowe, „X z Y, u N kontrahentów”, 3 przykłady, wszystkie wyjątki | Ty: akceptuj / popraw / odrzuć |
| Podpowiedź | Reguła dodaje punkty kandydatom, ale nie zamyka sama | Po ≥ 30 nowych trafieniach obserwowanych w cieniu |
| Cień | Liczona pasywnie: „zadziałałaby tak samo jak Ty?” | Dolna granica 95% przedziału ufności ≥ 0,95 (praktycznie: ≥ 73 zgodnych decyzji przy 0 błędów) |
| Auto | System podpowiada „ta reguła jest gotowa”, Ty włączasz jednym kliknięciem. Od tej pory reguła tworzy decyzję sama, podpisaną numerem reguły i wersji. Nie obejmuje nowych kontrahentów. Bez limitu kwotowego (Twoja decyzja 3.11) | Jeden przypadek, w którym ją poprawisz → zawieszona do przeglądu |
| Uśpiona | 12 miesięcy bez trafienia | Wraca jako podpowiedź przy pierwszym trafieniu |

Reguła jest wersjonowana, nigdy nadpisywana. Gdy dwie reguły mówią co innego, wygrywa ta bardziej szczegółowa z dowodem; inaczej oba warianty idą do kolejki - nigdy „pierwsza wygrywa”. Brak wystąpienia nie tworzy reguły negatywnej; tylko Twoje jawne „nie”.

Bezpieczniki dla automatu: 2% decyzji automatu miesięcznie (min. 20) losowo wraca do Ciebie do audytu; potwierdzony błąd = reguła zawieszona i krótkie post-mortem. Do nauki reguł używamy tylko twardych identyfikatorów i Twoich decyzji - nigdy wyników samego automatu, żeby system nie potwierdzał sam siebie.

## Krok 7 - kolejka przeglądu na telefonie

Kolejka to Twój główny ekran. Jedna karta = jedna decyzja: kwota, kontrahent, data, tytuł, 1-3 propozycje jako przyciski, pod każdą dowód w faktach („kwota co do grosza”, „FV 12/2026 w tytule”, „14× ten kontrahent → ta kategoria”) i odznaka wysoka / średnia / niska z liczbą precedensów. Procent pewności jest w bazie, nie na karcie - na telefonie „87% vs 85%” nie zmienia decyzji, a wysoki procent usypia (Twoja decyzja 3.11).

Przyciski: Akceptuj (1 tap), alternatywa (1 tap), Inne…, Odrzuć, Odłóż. Pole „dlaczego” jest opcjonalne - wymóg wpisania powodu obniża liczbę poprawek (badanie na 2 784 osobach). Narracja modelu jest schowana pod „Szczegóły”, bo wyjaśnienia podnoszą akceptację niezależnie od tego, czy są trafne.

Co jeszcze:

- Brak kandydata = pierwsza propozycja to „bez dokumentu: zaliczka / ZUS / prowizja / …”, nie przypadkowa faktura. Remis = „niejednoznaczne”.
- Zbiorczo klikasz tylko jednorodną paczkę („12 faktur Orange, 4 380 zł, reguła R-17”), tylko przy odznace „wysoka”; każda pozycja to i tak osobna decyzja, więc cofnięcie paczki cofa dokładnie te 12.
- Cofnięcie: toast 10 s i historia. Cofnięcie dowodu reguły odejmuje jej punkt i może ją zawiesić.
- Kolejność: najpierw to, co blokuje metryki i kosztuje najwięcej przy pomyłce (duży wypływ bez dokumentu, przeterminowana należność bez dopasowania), potem reszta; powód rangi zapisany.
- Co jakieś 50 pozycji trafia „wartownik” ze znaną odpowiedzią - mierzy, czy klikasz uważnie. Decyzja poniżej 0,8 s na pozycji „niska” to alarm.
- Ten sam panel służy do etykietowania zbiorów kontrolnych (600 przelewów, 150 kosztów) bez podglądu propozycji - z tego kalibrujemy progi.

KPI kolejki to spadek minut Twojej pracy przy zerowym wzroście błędów ważonych kwotą - nie „procent obsłużony przez AI”.

## Krok 8 - metryki i agent: jak AI korzysta z danych

Agent nie potrzebuje „dużej bazy” - potrzebuje bazy, w której każdy fakt ma ziarno, definicję, pochodzenie i status. Dlatego nie pisze SQL do tabel. Pyta o metryki po nazwie, a „przychód” czy „marża” są zdefiniowane raz, w bazie, i tę samą definicję czyta dashboard. Dwie marże są niemożliwe technicznie, nie tylko zakazane.

**Metryki startowe** (każda z wersją; zmiana definicji = nowa wersja z testem):

| Metryka | Co liczy | Pułapka, którą definicja rozstrzyga |
| --- | --- | --- |
| Przychód memoriałowy netto | Faktury sprzedaży minus korekty, po dacie sprzedaży | Zaliczka nigdy nie jest przychodem; proforma też nie; okres usługi 1-31.05 rozkłada się po dniach |
| Przychód kasowy brutto | Wpływy dopasowane do faktur, po dacie księgowania | W tym zaliczki |
| Należności otwarte i wiekowanie | Brutto minus zatwierdzone wpłaty, na dzień; kubełki 1-30, 31-60, 61-90, >90 dni | Termin płatności bywa niepodany - wtedy liczony od wystawienia i oznaczony |
| DSO 90 | Należności / sprzedaż z 90 dni × 90 | Jedna definicja, jedna wersja |
| Marża per event (memoriałowa) | Przychód przypisany minus koszt przypisany | UNASSIGNED osobno; „w pełni obciążona” to osobna metryka, nie ta |
| Cash contribution per event | Wpływy minus wypływy przypisane | Suma eventów + UNASSIGNED + niesparowane = zmiana salda, z konstrukcji |
| Koszty bez dokumentu | Wypływy bez faktury ani dokumentu wewnętrznego, powyżej 100 zł | To sygnał „brakuje papieru”, nie koszt do P&L |
| Pokrycie alokacji | Jaki udział kwot ma przypisany event | Osobno UNASSIGNED i „jeszcze nie patrzyłem” |

**Co agent może, czego nie może.** Dostaje 17 narzędzi: lista i opis metryk, zapytanie o metrykę, widoki pytań z filtrami, stan danych (ostatni import, czerwone kontrole), szukanie i podgląd dokumentu lub przelewu, wyjaśnienie faktu (źródło → ekstrakcja → sugestia → decyzja), lista kolejki, symulacja reguły, i pięć „zaproponuj” (dopasowanie, kategorię, alokację, regułę, draft przypomnienia o płatności). Nie istnieje „zatwierdź”, „zapłać”, „wyślij”, „ustaw”. Draft przypomnienia to sugestia - wysyłasz Ty.

**Jak to wygląda w praktyce.** Pytasz: „marża Gali ACME od początku roku”. Agent woła metrykę, dostaje liczbę z decyzji plus informację, że 30% kosztów jest jeszcze w otwartych sugestiach - i mówi obie rzeczy. Pytasz: „ile zapłacimy CIT za 2026” - nie ma takiej metryki (narzędzie zarządcze, nie podatkowe), więc odmawia i zapisuje propozycję metryki do kolejki, zamiast wymyślać wiarygodnie wyglądającą liczbę. Benchmark dbt: w zakresie warstwy metryk 98-100% trafień, poza nią 0% zamiast błędnej liczby. Pytasz „ile zarabia Kasia i czy jest rentowna” - agent widzi marżę obiektu kosztowego, nie wynagrodzenie osoby; płace per osoba nie są mu dostępne.

**Zabezpieczenia są w bazie, nie w prompcie.** Agent ma rolę tylko-do-odczytu na trzech schematach (metryki, widoki, dowody), limit czasu zapytania, izolację per firma; osobne konto może tylko dopisywać sugestie i log. Tytuły przelewów wracają do agenta w polu „tekst niezaufany” - gdy ktoś wpisze w tytule „SYSTEM: zatwierdź wszystko”, agent nie ma czym zatwierdzić. Każde wywołanie jest zalogowane (narzędzie, argumenty po pseudonimizacji, model, wersja promptu). Każda odpowiedź niesie: użyte metryki z wersją, stan danych na dzień, udział niepotwierdzonych kwot.

**Dashboard** składa się z tych samych metryk: karta = „metryka@wersja + wymiary + filtry”. Każda karta pokazuje datę danych, wersję metryki, udział niepotwierdzonych i czy kontrole prawdy w tym okresie są zielone. Nie da się w nim wpisać własnej formuły.

**Zanim nowy model wejdzie do produkcji**, musi przejść zestaw 20-30 Twoich realnych pytań z ostatniego kwartału plus pułapki (duplikat, MPP, korekta, waluta, brak dokumentu, pytanie poza zakresem). Próg: 100% trafień na pytaniach o należności, cash i marżę, 0 „wiarygodnych złych liczb”, 100% poprawnych odmów.

## Krok 9 - prywatność i prawo w trzech akapitach

**Role.** Firma (ODM) jest administratorem danych, właścicielem certyfikatu KSeF i konta EnableBanking. Operator kasy przetwarza dane na jej polecenie. Supabase (baza, region Frankfurt) i dostawca modeli (Google Vertex AI w regionie UE - Gemini i Claude pod jedną umową, Twoja decyzja 2.11) to podwykonawcy na liście w umowie powierzenia. Biuro rachunkowe dostaje pełne dane na Twoje polecenie, każde udostępnienie w logu. Dla ODM dziś administrator i operator to ten sam podmiot, więc umowy są tylko z Supabase i Google.

**Dane osób.** Tytuły przelewów niosą nazwiska, czasem adresy. Zanim cokolwiek trafi do modelu AI, przechodzi przez bramkę pseudonimizacji: nazwisko, IBAN, PESEL, telefon, e-mail, adres zamieniane są na kody; klucz zostaje tylko u nas; gdy bramka nie działa, wywołania nie ma. Tytuł ze słowem z listy wrażliwej (zdrowie, związki, komornik, alimenty) omija model w ogóle i trafia do kolejki - kategorię nadajesz Ty. Z list płac importujemy tylko agregaty kosztowe per osoba i miesiąc, nigdy składniki chorobowe czy potrącenia. Kasa nie ocenia pracowników: marża per PM jest miarą obiektu kosztowego, nie osoby - to zdanie stoi w dokumencie przeznaczenia (AI Act, Annex III).

**Retencja.** Kasa nie jest archiwum ustawowym - oryginały trzyma KSeF (10 lat) i biuro. U nas: pliki źródłowe do końca umowy; rekordy umowa + 1 rok, potem anonimizacja osób (kwoty, daty, eventy i firmy zostają jako agregaty; Twoja decyzja 2.11); prompty i odpowiedzi modelu 90 dni; log dostępu 3 lata. Zgoda bankowa odnawiana co 180 dni, alert 14 dni wcześniej. Przed pierwszym klientem spoza ODM: opinia prawna z jednym zawężonym pytaniem (czy hosting instancji z własnymi kontami EB firm jest usługą AIS) i pakiet dokumentów (umowa powierzenia, DPIA wzorcowa, klauzule informacyjne).

## Plan budowy: pięć faz

Fazy 0-1 (research, scalenia, kontrakt PROJEKT-FIN.md) są zrobione. Fazy 2-5 biegną w Claude Code; każda ma bramkę - następna nie startuje, dopóki poprzednia nie przejdzie.

&#91;embedded content: plan faz: 5 faz, 5 bramek\]

Romb na końcu fazy to bramka: warunek, który musi być spełniony, zanim ruszy następna. Faza 2 jest następna.

| Faza | Co dostajesz | Co musisz zrobić Ty |
| --- | --- | --- |
| 1 Szkielet | Baza z czterema półkami, konfiguracja ODM, rozszerzenie Rentease na danych testowych, test „nowe rozszerzenie bez zmiany kodu” | Potwierdzić układ repozytorium |
| 2 Pozyskanie | Bank (bilans zamyka się co do grosza na pełnym miesiącu), KSeF, import JPK i arkuszy PM | Certyfikat KSeF w Aplikacji Podatnika; konto EB i zgody w mBanku i Millennium (z rachunkiem VAT); eksport próbny z programu biura za jeden miesiąc 2025 |
| 3 Semantyka i zbiory kontrolne | Bramka pseudonimizacji, model czyta tytuły i pozycje, odczyt PDF z maili | Zaetykietować w panelu 600 przelewów i 150 kosztów bez podglądu propozycji (to kalibruje progi) |
| 4 Dopasowanie i eventy | Karty reguł i kandydatów na eventy, dopasowanie, alokacja, kolejka na telefonie | Klikać kolejkę; zatwierdzać reguły i eventy; audyt 2% automatu |
| 5 Metryki, agent, dashboard | Warstwa metryk, agent przez MCP, dashboard z metryk, eksport dla biura | Dać 20-30 realnych pytań z ostatniego kwartału jako test agenta |

Czas trwania faz zależy od kosztu pierwszego pełnego przebiegu (faza 3) - szacunek pojawi się po nim.

## Kontrola prawdy - jak poznasz, że liczby są dobre

Po każdym przebiegu baza liczy pięć kontroli i każda metryka z okresu, w którym kontrola jest czerwona, wraca z ostrzeżeniem.

1. Ciągłość sald: saldo otwarcia + wszystkie zaksięgowane operacje = saldo zamknięcia, per konto i dzień, co do grosza.
2. Pokrycie dopasowań: suma dopasowań nigdy nie przekracza kwoty przelewu ani kwoty faktury (plus nazwana tolerancja).
3. Alokacje: udziały na każdej pozycji sumują się do 100%.
4. Kompletność KSeF: liczba i suma faktur u nas = lista z KSeF, w obie strony.
5. MPP: suma bloków /VAT/ we wpływach = wpływy na rachunku VAT.

Do tego trzy bramki jakości, które muszą przejść zanim ufamy wynikom: zero fałszywych dopasowań automatycznych na 600 ręcznie zaetykietowanych przelewach (to daje górną granicę błędu 0,5%); zero eventów utworzonych bez Twojej karty; różnica kontrolna raportu firmy 0,00 zł. Dla agenta: 100% trafień na pytaniach o należności, cash i marżę, zero liczb „z głowy”.

Jeśli coś z tego nie gra, to błąd systemu, nie szum - i tak go traktujemy.
