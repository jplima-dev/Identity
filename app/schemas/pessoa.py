from pydantic import BaseModel, ConfigDict, Field

class PessoaBase(BaseModel):
# nome:
# idade:
#  comportamento:
#   Humor:
    pass


class CriarPessoa(PessoaBase):
    pass


class AtualizarPessoa(BaseModel):
    pass

class RespostaPessoa(PessoaBase):
    pass