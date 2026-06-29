import os
from dotenv import load_dotenv

load_dotenv()

SAP_AI_CONFIG = {
    "ai_api_url": os.getenv("SAP_AI_API_URL", "https://api.ai.prod.eu-central-1.aws.ml.hana.ondemand.com"),
    "client_id": os.getenv("SAP_AI_CLIENT_ID", ""),
    "client_secret": os.getenv("SAP_AI_CLIENT_SECRET", ""),
    "auth_url": os.getenv("SAP_AI_AUTH_URL", "https://infy-sap-nts-ai.authentication.eu10.hana.ondemand.com"),
    "resource_group": os.getenv("SAP_AI_RESOURCE_GROUP", "default"),
    "model_name": os.getenv("SAP_AI_MODEL_NAME", "gpt-4o"),
}
