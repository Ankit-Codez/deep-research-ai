"""Login contracts normalize identifiers, never the user's password."""
from uuid import UUID
from pydantic import EmailStr, Field, SecretStr, field_validator
from deepresearch_ai.models.research import ResearchModel


class LoginRequest(ResearchModel):
    email: EmailStr = Field(max_length=254)
    password: SecretStr = Field(min_length=1, max_length=128)

    @field_validator('email', mode='before')
    @classmethod
    def normalize_email(cls, value):
        return value.strip().casefold() if isinstance(value, str) else value


class RegisterRequest(LoginRequest):
    name: str = Field(min_length=1, max_length=80)
    password: SecretStr = Field(min_length=15, max_length=128)

    @field_validator('name')
    @classmethod
    def clean_name(cls, value):
        value = value.strip()
        if not value: raise ValueError('Name cannot be blank.')
        return value


class UserPublic(ResearchModel):
    id: UUID
    email: str
    name: str
    is_admin: bool = False


class AuthResponse(ResearchModel):
    user: UserPublic
    csrf_token: str
