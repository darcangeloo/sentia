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

oauth2_scheme = OAuth2PasswordBearer(tokenUrl="login")


def hash_password(password: str) -> str:
    """Hash di una password con bcrypt e salt automatico."""
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(password.encode('utf-8'), salt).decode('utf-8')


def verify_password(plain_password: str, hashed_password: str) -> bool:
    """Verifica una password in chiaro contro il suo hash bcrypt."""
    return bcrypt.checkpw(plain_password.encode('utf-8'), hashed_password.encode('utf-8'))


def create_access_token(data: dict) -> str:
    """Crea un JWT token con scadenza configurabile via .env."""
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + timedelta(hours=settings.TOKEN_EXPIRY_HOURS)
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=ALGORITHM)


def get_current_tenant(token: str = Depends(oauth2_scheme)) -> dict:
    """Estrae e valida i dati del tenant dal JWT token.
    
    Returns:
        dict con 'company_id' e 'user_id' estratti dal token.
    
    Raises:
        HTTPException 401 se il token è invalido, scaduto o mancante.
    """
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Token non valido o scaduto",
    )
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[ALGORITHM])
        company_id: str = payload.get("company_id")
        user_id: str = payload.get("user_id")
        if not company_id or not user_id:
            raise credentials_exception
        return {"company_id": company_id, "user_id": user_id}
    except JWTError:
        logger.warning("Tentativo di accesso con token JWT invalido")
        raise credentials_exception