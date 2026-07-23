# Fonti e lacune

## Integrate in questa versione

| Livello | Fonte | Granularità | Nota |
|---|---|---|---|
| 12 indicatori di accessibilità | OpenStreetMap (Overpass), luglio 2026 | comune | tempo stimato in auto dal centro comunale |
| Popolazione, struttura per età | Istat POSAS, 1.1.2025 e 1.1.2019 | comune | variazione 2019–2025, indice di vecchiaia, età media |
| Esposizione ad alluvione e frana | Ispra IdroGEO (mosaicatura PAI) | comune | % residenti in area P3 / P2 e in area frana P3-P4 |
| Reddito imponibile per contribuente | Istat «A misura di Comune» tav. 2.1, su dati MEF 2022 | comune | solo comuni sopra 5.000 abitanti nelle fonti ARCH.I.M.E.DE |
| Pressione turistica | OpenStreetMap `tourism=*` dentro i confini comunali | comune | esercizi, non posti letto |
| Distanza dal mare e dalla spiaggia | OpenStreetMap `natural=coastline` / `natural=beach` | comune | nel delta la linea di costa segue anche le sponde lagunari |
| Quota del centro comunale | EU-DEM 25 m (OpenTopoData) | comune | usata per stimare la velocità media locale |

## Non reperibile come open data leggibile da questo ambiente

| Livello | Dove vive | Perché serve |
|---|---|---|
| **Subsidenza** | Rete di capisaldi Arpae / Regione Emilia-Romagna, mappe isocinetiche; il WMS regionale richiede autenticazione | Sul litorale ferrarese e ravennate l'abbassamento del suolo è il vero moltiplicatore del rischio idraulico |
| **Erosione costiera** | Arpae, *Stato del litorale emiliano-romagnolo*; indicatori costa nel catalogo RNDT senza URL di download diretto | Determina la larghezza della spiaggia e il costo dei ripascimenti, quindi anche le tariffe locali |
| **Classificazione sismica / PGA** | Dipartimento Protezione Civile (elenco comuni) e griglia INGV MPS04 a 5 km; il geoserver INGV non risponde in TLS da qui | Dopo il 2012 conta per assicurazione e costi di adeguamento |
| **Copertura FTTH / banda ultralarga** | Mappa AGCOM e Infratel, consultabili solo via interfaccia; nessun dataset nazionale per comune nel catalogo dati.gov.it | Per chi lavora da remoto è probabilmente la variabile decisiva |
| **Prezzi immobiliari** | Quotazioni OMI, Agenzia delle Entrate, per zona OMI | Nessuna decisione affitto/acquisto/piazzola regge senza |
| **Stagionalità dei servizi** | Nessuna fonte sistematica: sulla costa molti esercizi chiudono da ottobre ad aprile | Il conteggio di luglio sovrastima l'offerta invernale |

Se una di queste diventa disponibile, il modo di integrarla è lo stesso: aggiungere la colonna a
`indicators_plus.csv`, inserirla in `CORE`, `OPTIONAL` o `CONTEXT` dentro `build_indice_servizi.py`
e rilanciare lo script, che riscrive da solo il payload dentro l'HTML.
