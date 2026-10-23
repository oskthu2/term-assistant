"""Skapa en exempel-Excel som liknar ett typiskt kodverk i kalkylark."""
from datetime import date
from openpyxl import Workbook

wb = Workbook()
ws = wb.active
ws.title = "Kodverk"
ws.append(["Kodverk för vårdkontaktens orsak, version 3"])
ws.append([])
ws.append(["Kod", "Benämning", "Beskrivning", "Överordnad kod", "Benämning (en)",
           "Synonymer", "Giltig fr.o.m.", "Inaktiv", "Anmärkning", "Sorteringsordning", "Ansvarig enhet", "Tom"])
rows = [
    ("A", "Akut", "Oplanerad kontakt", None, "Acute", None, date(2020, 1, 1), "Nej", None, 1, "Enhet 1"),
    ("A1", "Akut sjukdom", "Akut insjuknande", "A", "Acute illness", "Akut insjuknande", date(2020, 1, 1), "Nej", None, 2, "Enhet 1"),
    ("A2", "Olycksfall", "Skada genom olycka", "A", "Accident", "Olycka;Trauma", date(2020, 1, 1), "Nej", "Se även B2", 3, "Enhet 2"),
    ("P", "Planerad", "Planerad kontakt", None, "Planned", None, date(2020, 1, 1), "Nej", None, 4, "Enhet 1"),
    ("P1", "Återbesök", "Planerat återbesök", "P", "Follow-up", "Uppföljning", date(2021, 3, 1), "Nej", None, 5, "Enhet 2"),
    ("P2", "Screening", "Kallelse till screening", "P", "Screening", None, date(2021, 3, 1), "Ja", "Utgår 2024", 6, "Enhet 2"),
]
for r in rows:
    ws.append(list(r))
wb.save("examples/vardkontaktorsak.xlsx")
print("Skrev examples/vardkontaktorsak.xlsx")
