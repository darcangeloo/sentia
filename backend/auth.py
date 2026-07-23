import logging
import bcrypt
from datetime import datetime, timedelta, timezone
from fastapi import Depends, HTTPException, status
from fastapi.security import OAuth2PasswordBearer
from jose import jwt, JWTError
from backend.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

ALGORITHM = "HS256"

# Scope di un token "provvisorio": emesso al login quando l'utente deve ancora
# cambiare la password al primo accesso. Vale SOLO per l'endpoint di cambio
# password; get_current_tenant lo rifiuta, quindi nessuna operazione normale è
# raggiungibile con la password provvisoria.
PASSWORD_CHANGE_SCOPE = "password_change"

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


def hash_password(password: str) -> str:
    """Hash di una password con bcrypt e salt automatico."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica una password in chiaro contro il suo hash bcrypt."""
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))


def create_access_token(data: dict, scope: str | None = None) -> str:
    """Crea un JWT token con scadenza configurabile via .env.

    Se `scope` è valorizzato (es. PASSWORD_CHANGE_SCOPE) viene incluso nel
    token e limita ciò che il token può fare (vedi get_current_tenant).
    """
    to_encode = data.copy()
    if scope:
        to_encode["scope"] = scope
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.TOKEN_EXPIRY_HOURS)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=ALGORITHM)


def _decode_tenant(token: str) -> dict:
    """Decodifica e valida il JWT, restituendo company_id, user_id e scope.

    Non applica alcuna politica sullo scope: quella spetta ai dependency che
    lo usano (get_current_tenant lo blocca, il cambio password lo accetta).
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token non valido o scaduto",
    )
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[ALGORITHM])
    except JWTError:
        logger.warning("Tentativo di accesso con token JWT invalido")
        raise credentials_exception
    company_id: str = payload.get("company_id")
    user_id: str = payload.get("user_id")
    if not company_id or not user_id:
        raise credentials_exception
    return {"company_id": company_id, "user_id": user_id, "scope": payload.get("scope")}


def get_current_tenant(token: str = Depends(oauth2_scheme)) -> dict:
    """Estrae e valida i dati del tenant dal JWT token.

    Returns:
        dict con 'company_id' e 'user_id' estratti dal token.

    Raises:
        HTTPException 401 se il token è invalido, scaduto o mancante.
        HTTPException 403 se il token è "provvisorio" (cambio password
        obbligatorio non ancora effettuato): in quel caso nessuna operazione
        normale è consentita finché la password non viene cambiata.
    """
    data = _decode_tenant(token)
    if data.get("scope") == PASSWORD_CHANGE_SCOPE:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Cambio password obbligatorio prima di accedere.",
        )
    return {"company_id": data["company_id"], "user_id": data["user_id"]}


def get_tenant_for_password_change(token: str = Depends(oauth2_scheme)) -> dict:
    """Dependency per il solo endpoint di cambio password.

    Accetta sia il token provvisorio (scope=password_change) sia un token
    normale: cambiare la propria password è lecito in entrambi i casi.
    """
    data = _decode_tenant(token)
    return {"company_id": data["company_id"], "user_id": data["user_id"]}