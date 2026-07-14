-- Aggiunge una colonna per memorizzare il motivo del fallimento di un documento,
-- cosi' che un errore di embedding (es. HF router che torna 500 ripetutamente)
-- sia visibile in modo leggibile invece di lasciare il documento bloccato
-- in "processing" senza spiegazione.
ALTER TABLE documents ADD COLUMN IF NOT EXISTS error_message TEXT;
