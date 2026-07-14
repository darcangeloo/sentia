import logging
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from sqlalchemy import Column, String, ForeignKey, TEXT, TIMESTAMP, LargeBinary, Integer, Index, Boolean, text as sqlalchemy_text
from sqlalchemy.dialects.postgresql import UUID
from backend.config import get_settings

logger = logging.getLogger(__name__)
settings = get_settings()

engine = create_async_engine(
    settings.DATABASE_URL, 
    echo=False,
    pool_size=settings.DB_POOL_SIZE,
    max_overflow=settings.DB_MAX_OVERFLOW,
    pool_pre_ping=True,  # Verifica connessioni stale prima dell'uso
)
AsyncSessionLocal = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
Base = declarative_base()


class Company(Base):
    __tablename__ = "companies"
    id = Column(UUID(as_uuid=True), primary_key=True)
    name = Column(String(255), nullable=False)
    llm_provider = Column(String(50))
    encrypted_api_key = Column(LargeBinary)
    created_at = Column(TIMESTAMP, server_default="NOW()")
    
    # Relationships
    users = relationship("User", back_populates="company", cascade="all, delete-orphan")
    documents = relationship("Document", back_populates="company", cascade="all, delete-orphan")


class User(Base):
    __tablename__ = "users"
    id = Column(UUID(as_uuid=True), primary_key=True)
    email = Column(String(255), unique=True, nullable=False)
    password_hash = Column(TEXT, nullable=False)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"))
    role = Column(String(20), default="user")
    
    # Relationships
    company = relationship("Company", back_populates="users")


class Document(Base):
    __tablename__ = "documents"
    id = Column(UUID(as_uuid=True), primary_key=True)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"))
    filename = Column(String(500))
    storage_path = Column(TEXT)
    status = Column(String(20), default="processing")  # processing, ready, error
    error_message = Column(TEXT, nullable=True)
    page_count = Column(Integer, nullable=True)
    chunk_count = Column(Integer, nullable=True, default=0)
    created_at = Column(TIMESTAMP, server_default="NOW()")
    
    # Relationships
    company = relationship("Company", back_populates="documents")
    chunks = relationship("Chunk", back_populates="document", cascade="all, delete-orphan")
    
    # Indici per query performanti
    __table_args__ = (
        Index("idx_documents_company_id", "company_id"),
    )


class Chunk(Base):
    """Modello ORM per i chunks vettoriali.
    
    Nota: la colonna 'embedding' è di tipo vector(768) in PostgreSQL (pgvector).
    SQLAlchemy non ha un tipo nativo per vector, quindi usiamo TEXT per l'ORM
    e raw SQL per le operazioni vettoriali (insert con cast, cosine distance).
    """
    __tablename__ = "chunks"
    id = Column(UUID(as_uuid=True), primary_key=True)
    document_id = Column(UUID(as_uuid=True), ForeignKey("documents.id", ondelete="CASCADE"))
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"))
    text = Column(TEXT, nullable=False)
    page_number = Column(Integer, nullable=True)  # Pagina di provenienza
    chunk_index = Column(Integer, nullable=True)   # Posizione nel documento
    # embedding: vector(768) — gestito via raw SQL, non come colonna ORM
    
    # Relationships
    document = relationship("Document", back_populates="chunks")
    
    __table_args__ = (
        Index("idx_chunks_company_id", "company_id"),
        Index("idx_chunks_document_id", "document_id"),
    )


class ChatMessage(Base):
    __tablename__ = "chat_messages"
    
    id = Column(UUID(as_uuid=True), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    conversation_id = Column(UUID(as_uuid=True), ForeignKey("conversations.id", ondelete="CASCADE"), nullable=True)
    role = Column(String(20), nullable=False)  # 'user' o 'assistant'
    content = Column(TEXT, nullable=False)
    sources_json = Column(TEXT, nullable=True)  # JSON delle fonti usate per la risposta
    created_at = Column(TIMESTAMP, server_default="NOW()")
    
    # Relationships
    conversation = relationship("Conversation", back_populates="messages")
    
    __table_args__ = (
        Index("idx_chat_user_id", "user_id"),
        Index("idx_chat_company_id", "company_id"),
        Index("idx_chat_conversation_id", "conversation_id"),
    )


class Conversation(Base):
    __tablename__ = "conversations"
    
    id = Column(UUID(as_uuid=True), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    title = Column(String(255), nullable=False)
    created_at = Column(TIMESTAMP, server_default="NOW()")
    updated_at = Column(TIMESTAMP, server_default="NOW()")
    
    # Relationships
    messages = relationship("ChatMessage", back_populates="conversation", cascade="all, delete-orphan")
    
    __table_args__ = (
        Index("idx_conversations_user_id", "user_id"),
        Index("idx_conversations_company_id", "company_id"),
    )


class UserLLMSetting(Base):
    __tablename__ = "user_llm_settings"
    
    id = Column(UUID(as_uuid=True), primary_key=True)
    user_id = Column(UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    company_id = Column(UUID(as_uuid=True), ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    provider = Column(String(50), nullable=False)  # 'openai', 'anthropic', 'gemini'
    encrypted_api_key = Column(LargeBinary, nullable=True)
    base_url = Column(String(500), nullable=True)
    model = Column(String(255), nullable=False)
    is_active = Column(Boolean, default=False)
    created_at = Column(TIMESTAMP, server_default="NOW()")
    updated_at = Column(TIMESTAMP, server_default="NOW()")
    
    __table_args__ = (
        Index("idx_user_llm_settings_user_id", "user_id"),
        Index("idx_user_llm_settings_company_id", "company_id"),
        Index("idx_user_llm_settings_user_provider", "user_id", "provider", unique=True),
    )


async def verify_and_migrate_db():
    """Verifica lo stato del database e applica le migrazioni per le nuove tabelle/colonne."""
    logger.info("Verifica e migrazione database in corso...")
    async with engine.begin() as conn:
        # Crea la tabella conversations se non esiste
        await conn.execute(sqlalchemy_text("""
            CREATE TABLE IF NOT EXISTS conversations (
                id UUID PRIMARY KEY,
                user_id UUID REFERENCES users(id) ON DELETE CASCADE NOT NULL,
                company_id UUID REFERENCES companies(id) ON DELETE CASCADE NOT NULL,
                title VARCHAR(255) NOT NULL,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW()
            )
        """))
        
        # Aggiungi conversation_id a chat_messages se non esiste
        await conn.execute(sqlalchemy_text("""
            ALTER TABLE chat_messages 
            ADD COLUMN IF NOT EXISTS conversation_id UUID REFERENCES conversations(id) ON DELETE CASCADE
        """))
        
        # Crea la tabella user_llm_settings se non esiste
        await conn.execute(sqlalchemy_text("""
            CREATE TABLE IF NOT EXISTS user_llm_settings (
                id UUID PRIMARY KEY,
                user_id UUID REFERENCES users(id) ON DELETE CASCADE NOT NULL,
                company_id UUID REFERENCES companies(id) ON DELETE CASCADE NOT NULL,
                provider VARCHAR(50) NOT NULL,
                encrypted_api_key BYTEA,
                base_url VARCHAR(500),
                model VARCHAR(255) NOT NULL,
                is_active BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                UNIQUE (user_id, provider)
            )
        """))
        
        # Crea gli indici se non esistono
        await conn.execute(sqlalchemy_text("CREATE INDEX IF NOT EXISTS idx_conversations_user_id ON conversations(user_id)"))
        await conn.execute(sqlalchemy_text("CREATE INDEX IF NOT EXISTS idx_conversations_company_id ON conversations(company_id)"))
        await conn.execute(sqlalchemy_text("CREATE INDEX IF NOT EXISTS idx_chat_messages_conversation_id ON chat_messages(conversation_id)"))
        await conn.execute(sqlalchemy_text("CREATE INDEX IF NOT EXISTS idx_user_llm_settings_user_id ON user_llm_settings(user_id)"))
        
    logger.info("✅ Migrazione database completata con successo!")