from pydantic import BaseModel, ConfigDict 
from datetime import datetime

class OrderSchema(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    order_id: int
    telegram_id: int
    plan_id: str
    requested_username: str
    panel_username: str
    created_at: datetime
    expire_at: datetime
