from pydantic import BaseModel, Field
from typing import Literal

class ResponseSchema(BaseModel):
    result: Literal["success", "failure"] = Field(default="success", description="Response success or failure")

