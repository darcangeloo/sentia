"""Configurazione condivisa dei test.

I moduli del backend chiamano get_settings() al momento dell'import, e alcune
impostazioni (DATABASE_URL, JWT_SECRET_KEY, MASTER_KEY) sono obbligatorie senza
default. Qui si popolano con valori fittizi PRIMA che qualunque test importi un
modulo backend, così l'import non fallisce. I valori non aprono connessioni:
l'engine SQLAlchemy è creato in modo lazy e nessun test tocca il database reale.
"""
import os

os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("JWT_SECRET_KEY", "test-secret-key-not-used-in-production")
# MASTER_KEY deve essere una chiave Fernet valida (32 byte url-safe base64):
# la usa backend/crypto.py all'import. Questa è una chiave di test generata a
# scopo esclusivo di CI, non protegge nulla di reale.
os.environ.setdefault("MASTER_KEY", "xTnph2jNs3BbSp3cYelMSmcgDXL7APva7HoQ0gHcgvk=")
os.environ.setdefault("GEMINI_EMBEDDING_API_KEY", "test-key")
