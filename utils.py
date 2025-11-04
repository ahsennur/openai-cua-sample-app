import os
import requests
from dotenv import load_dotenv
import json
import base64
from PIL import Image
from io import BytesIO
import io
from urllib.parse import urlparse

load_dotenv(override=True)

BLOCKED_DOMAINS = [
    "maliciousbook.com",
    "evilvideos.com",
    "darkwebforum.com",
    "shadytok.com",
    "suspiciouspins.com",
    "ilanbigio.com",
]


def pp(obj):
    print(json.dumps(obj, indent=4))


def show_image(base_64_image):
    image_data = base64.b64decode(base_64_image)
    image = Image.open(BytesIO(image_data))
    image.show()


def calculate_image_dimensions(base_64_image):
    image_data = base64.b64decode(base_64_image)
    image = Image.open(io.BytesIO(image_data))
    return image.size


def sanitize_message(msg: dict) -> dict:
    """Return a copy of the message with image_url omitted for computer_call_output messages."""
    if msg.get("type") == "computer_call_output":
        output = msg.get("output", {})
        if isinstance(output, dict):
            sanitized = msg.copy()
            sanitized["output"] = {**output, "image_url": "[omitted]"}
            return sanitized
    return msg


def create_response(index, **kwargs):
    import time
    import requests
    from requests.exceptions import ConnectionError, Timeout, RequestException
    
    url = "https://api.openai.com/v1/responses"
    headers = {
        "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
        "Content-Type": "application/json"
    }

    openai_org = os.getenv("OPENAI_ORG")
    if openai_org:
        headers["Openai-Organization"] = openai_org

    # Retry configuration
    max_retries = 3
    base_delay = 2  # seconds
    max_delay = 60  # seconds
    
    for attempt in range(max_retries + 1):
        try:
            # Add timeout to prevent hanging requests
            response = requests.post(url, headers=headers, json=kwargs, timeout=60)
            
            # Handle different HTTP status codes
            if response.status_code == 200:
                # Success case
                with open(f"outputs/api_calls/call_{index}.json", "w+") as file:
                    file.write(str(response.json()).replace("'","\""))
                return response.json()
                
            elif response.status_code == 500:
                # Server error - retry with exponential backoff
                print(f"🔄 OpenAI Server Error (500) on attempt {attempt + 1}/{max_retries + 1}")
                if attempt < max_retries:
                    delay = min(base_delay * (2 ** attempt), max_delay)
                    print(f"⏳ Retrying in {delay} seconds...")
                    time.sleep(delay)
                    continue
                else:
                    print(f"❌ Max retries exceeded for server error")
                    response.raise_for_status()
                    
            elif response.status_code == 429:
                # Rate limit - longer backoff
                print(f"🚦 Rate limit hit on attempt {attempt + 1}/{max_retries + 1}")
                if attempt < max_retries:
                    delay = min(base_delay * (3 ** attempt), max_delay)  # More aggressive backoff
                    print(f"⏳ Rate limit backoff: {delay} seconds...")
                    time.sleep(delay)
                    continue
                else:
                    print(f"❌ Max retries exceeded for rate limit")
                    response.raise_for_status()
                    
            else:
                # Other HTTP errors
                print(f"❌ HTTP Error {response.status_code}: {response.text}")
                response.raise_for_status()
                
        except (ConnectionError, Timeout) as e:
            # Network connectivity issues
            print(f"🌐 Network error on attempt {attempt + 1}/{max_retries + 1}: {type(e).__name__}")
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                print(f"⏳ Network retry in {delay} seconds...")
                time.sleep(delay)
                continue
            else:
                print(f"❌ Max retries exceeded for network error")
                raise e
                
        except RequestException as e:
            # Other request-related errors
            print(f"🔧 Request error on attempt {attempt + 1}: {e}")
            if attempt < max_retries:
                delay = min(base_delay * (2 ** attempt), max_delay)
                print(f"⏳ Request error retry in {delay} seconds...")
                time.sleep(delay)
                continue
            else:
                print(f"❌ Max retries exceeded for request error")
                raise e
        
        except Exception as e:
            # Unexpected errors
            print(f"⚠️ Unexpected error on attempt {attempt + 1}: {e}")
            raise e
    
    # This shouldn't be reached, but just in case
    raise Exception("Unexpected end of retry loop")


def check_blocklisted_url(url: str) -> None:
    """Raise ValueError if the given URL (including subdomains) is in the blocklist."""
    hostname = urlparse(url).hostname or ""
    if any(
        hostname == blocked or hostname.endswith(f".{blocked}")
        for blocked in BLOCKED_DOMAINS
    ):
        raise ValueError(f"Blocked URL: {url}")
