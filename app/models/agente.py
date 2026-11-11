from datetime import datetime

from sqlalchemy import Integer, String, DateTime, ForeignKey, JSON, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.conection import Base

class Agente(Base):
    __tablename__ = "agentes"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    nome: Mapped[str] = mapped_column(String(100), nullable=False)
    idade: Mapped[int] = mapped_column(nullable=False)
    descricao: Mapped[str] = mapped_column(String(255), nullable=True)

    profissao: Mapped(str) = mapped_column(String(50), nullable=False)
    comportamento: Mapped[str] = mapped_column(String(30), nullable=False)
    humor: Mapped[str] = mapped_column(String(30), nullable=False)

    criado_em: Mapped[datetime] = mapped_column(DateTime, server_dafault=func.now())