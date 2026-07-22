# Eval harness del retrieval RAG

Misura la qualità del retrieval con metriche standard (recall@k, precision@k,
MRR) su un *golden set* di domande con i documenti attesi. Serve a trasformare
le modifiche alla pipeline (chunking, fusione RRF, soglie) da scommesse in
scelte misurabili, e a intercettare le regressioni.

## Esecuzione offline (demo / smoke test)

```bash
python -m backend.eval.evaluate
```

Usa un retriever fittizio incluso: non tocca né rete né database. Dimostra
l'harness e viene esercitato dai test in `backend/tests/test_eval_metrics.py`.

## Valutare il sistema reale

`evaluate()` accetta una funzione `retriever(query) -> list[str]` che
restituisce gli id ordinati per rilevanza. Per misurare il retrieval vero,
passa un retriever che interroga Postgres/embedding (es. adattando
`backend.rag._retrieve_hybrid`) e usa come id la coppia `filename#chunk_index`.
Poi popola un golden set reale con domande rappresentative e i documenti attesi.

## Formato del golden set (JSONL)

Una riga JSON per query:

```json
{"query": "elencami tutti i pagamenti a Rossi Srl", "relevant": ["estratto_marzo.pdf#mov12", "estratto_aprile.pdf#mov3"]}
```

- `query`: la domanda dell'utente.
- `relevant`: gli id dei chunk/documenti che una risposta corretta deve usare.

## Metriche

- **recall@k**: dei documenti attesi, quanti compaiono nei primi k risultati.
  La metrica chiave per le query esaustive ("tutti i pagamenti a X"), dove
  un'omissione è un errore silenzioso.
- **precision@k**: dei primi k risultati, quanti sono davvero rilevanti.
- **MRR**: quanto in alto arriva il primo risultato rilevante. Conta soprattutto
  per le query di tipo lookup ("qual è il saldo?"), dove serve il primo colpo.
