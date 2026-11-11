from sqlalchemy import Integer, String, DateTime, ForeignKey, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database.conection import Base

class Personalidade(Base):
    __tablename__ = "personalidade"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)