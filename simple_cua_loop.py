"""A script that runs a Computer Use Assistant (CUA) loop using Local Playwright."""

import os
import glob
import time
import csv
import base64
import subprocess
import sys
import json
import re
import requests
import tempfile
from computers import Computer
from computers import LocalPlaywrightComputer
from utils import create_response, check_blocklisted_url

INITIAL_URL = "https://apps.labor.ny.gov/UIEHP/"

def acknowledge_safety_check_callback(message: str) -> bool:
    """
        Acknowledge a safety check.
    """
    response = input(
        f"Safety Check Warning: {message}\nDo you want to acknowledge and proceed? (y/n): "
    ).lower()
    return response.strip() == "y"


def handle_item(item, computer: Computer):
    """Handle each item; may cause a computer action + screenshot."""
    if item["type"] == "message":  # print messages
        print(item["content"][0]["text"])

    if item["type"] == "computer_call":  # perform computer actions
        action = item["action"]
        action_type = action["type"]
        action_args = {k: v for k, v in action.items() if k != "type"}
        print(f"{action_type}({action_args})")

        # give our computer environment action to perform
        getattr(computer, action_type)(**action_args)

        screenshot_base64 = computer.screenshot()

        pending_checks = item.get("pending_safety_checks", [])
        for check in pending_checks:
            if not acknowledge_safety_check_callback(check["message"]):
                raise ValueError(f"Safety check failed: {check['message']}")

        # return value informs model of the latest screenshot
        call_output = {
            "type": "computer_call_output",
            "call_id": item["call_id"],
            "acknowledged_safety_checks": pending_checks,
            "output": {
                "type": "input_image",
                "image_url": f"data:image/png;base64,{screenshot_base64}",
            },
        }

        # additional URL safety checks for browser environments
        if computer.environment == "browser":
            current_url = computer.get_current_url()
            call_output["output"]["current_url"] = current_url
            check_blocklisted_url(current_url)

        return [call_output]

    return []


def write_to_csv(time_taken: float, zp_company_id: str, status: str):
    """
        Write the time taken and company details to a CSV file.
    """
    csv_file = "outputs/response_times.csv"
    file_exists = os.path.isfile(csv_file)
    with open(csv_file, 'a', newline='', encoding='utf-8') as f:
        writer = csv.writer(f)
        if not file_exists:
            writer.writerow(['Start Time', 'Time Taken (seconds)', 'ZP Company ID', 'Status'])
        writer.writerow([time.strftime('%Y-%m-%d %H:%M:%S'), time_taken, zp_company_id, status])


def log_failure(zp_company_id: str, reason: str):
    """Log failed searches to a separate file."""
    log_file = "outputs/failed_searches.log"
    with open(log_file, 'a', encoding='utf-8') as f:
        timestamp = time.strftime('%Y-%m-%d %H:%M:%S')
        f.write(f"[{timestamp}] ZP Company ID: {zp_company_id} - {reason}\n")


def search_company(company_data: dict):
    """First stage: Search for the company using separate agent instance."""
    try:
        # Create a fresh browser instance for search
        with LocalPlaywrightComputer(initial_url=INITIAL_URL) as computer:
            tools = [
                {
                    "type": "computer-preview",
                    "display_width": computer.dimensions[0],
                    "display_height": computer.dimensions[1],
                    "environment": computer.environment,
                }
            ]

            # Initialize items
            items = []
            i = 1

            # Read and process the search-only prompt template
            fein = company_data['ZP FEIN']
            state_ein = company_data['ZP State EIN']

            with open("input/search_only_prompt.txt", "r") as f:
                search_prompt_template = f.read()
                search_prompt = search_prompt_template.format(
                    fein=fein,
                    state_ein=state_ein
                )

            # Use only the search prompt
            items.append({"role": "user", "content": search_prompt})

            # keep looping until we get a final response
            while True:
                response = create_response(
                    index=i,
                    model="computer-use-preview",
                    input=items,
                    tools=tools,
                    truncation="auto",
                    include=["reasoning.encrypted_content"]
                )

                # Check if the response is valid
                if "output" not in response:
                    print(response)
                    error_type = response.get('error', {}).get('type', '')
                    error_message = response.get('error', {}).get('message', '')
                    if error_type:
                        print(f"API Error: {error_type}: {error_message}")
                        return False, None, None
                    else:
                        print(f"API Error: {error_message}")
                        return False, None, None

                # Add the response to the items to maintain context & compliance
                items += response["output"]
                
                # DEBUG: Show response before handling
                print(f"\n🔍 DEBUG: Response received (iteration {i}):")
                for item in response["output"]:
                    if item.get("type") == "message":
                        print(f"📝 Message: {item['content'][0]['text'][:100]}...")
                    elif item.get("type") == "computer_call":
                        action = item["action"]
                        print(f"🖱️ Action: {action['type']} {action}")

                # Handle each item
                for item in response["output"]:
                    items += handle_item(item, computer)

                # Check if the last item is an assistant response
                if items[-1].get("role") == "assistant":
                    # Parse the assistant's output as JSON
                    assistant_output = items[-1]["content"][0]["text"]
                    try:
                        data = json.loads(assistant_output.replace("'", '"'))
                        # Check if search was successful
                        if data.get("status") == "failed":
                            return False, None, None
                        elif data.get("status") == "success":
                            # Take screenshot for UDU extraction
                            print("✅ Company page reached, waiting for page to load completely...")
                            screenshot_base64 = computer.screenshot()
                            print("📸 Screenshot captured for UDU extraction")
                            
                            # Save screenshot to file for inspection
                            screenshot_data = base64.b64decode(screenshot_base64)
                            screenshot_filename = f"outputs/screenshot_{company_data['ZP Company ID']}.png"
                            with open(screenshot_filename, "wb") as f:
                                f.write(screenshot_data)
                            print(f"💾 Screenshot saved to: {screenshot_filename}")
                            
                            return True, data, screenshot_base64
                        else:
                            # Unknown status, treat as failed
                            return False, None, None
                    except Exception as e:
                        # If parsing fails, treat as failed
                        print(f"JSON parsing error: {e}")
                        print(f"Assistant output: {assistant_output}")
                        return False, None, None

                i += 1
    except Exception as e:
        print(f"Search error: {e}")
        return False, None, None


def extract_company_info_with_udu(company_data: dict, screenshot_base64: str):
    """Second stage: Extract company information using OpenAI Vision API with provided screenshot."""
    zp_company_id = company_data['ZP Company ID']
    
    try:
        
        # Read UDU prompt from file
        with open("input/extract_only_prompt.txt", "r") as f:
            udu_prompt = f.read()
        
        print("🔄 Calling OpenAI Vision API for extraction...")
        
        # Prepare OpenAI Vision API payload
        headers = {
            "Authorization": f"Bearer {os.getenv('OPENAI_API_KEY')}",
            "Content-Type": "application/json"
        }
        
        # Prepare OpenAI Vision API payload
        udu_payload = {
            "model": "gpt-4.5",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": udu_prompt
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:image/png;base64,{screenshot_base64}"
                            }
                        }
                    ]
                }
            ],
            "max_tokens": 1000,
            "temperature": 0
        }
        
        # Call OpenAI Vision API directly
        udu_response = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers=headers,
            json=udu_payload,
            timeout=60
        )
        
        if udu_response.status_code != 200:
            print(f"OpenAI Vision API Error: {udu_response.status_code} {udu_response.text}")
            raise ValueError("OpenAI Vision API call failed")
        
        udu_response_data = udu_response.json()
        udu_output = udu_response_data["choices"][0]["message"]["content"]
        
        print("✅ OpenAI Vision API call completed")
        print(f"🔍 UDU Raw Output: {udu_output[:200]}...")
        
        # Clean markdown formatting if present
        if udu_output.startswith("```json"):
            udu_output = udu_output[7:]  # Remove ```json
        if udu_output.startswith("```"):
            udu_output = udu_output[3:]  # Remove ```
        if udu_output.endswith("```"):
            udu_output = udu_output[:-3]  # Remove trailing ```
        udu_output = udu_output.strip()  # Remove whitespace

        try:
            data = json.loads(udu_output.replace("'", '"'))
            
            # Process the extracted data
            if "sui_rate" in data:
                try:
                    rate = str(data["sui_rate"]).replace("%", "").strip()
                    data["sui_rate"] = float(rate) / 100
                except Exception:
                    pass
            if "state_ein" in data:
                data["state_ein"] = re.sub(r"\D", "", str(data["state_ein"]))
            if "fein" in data:
                data["fein"] = re.sub(r"\D", "", str(data["fein"]))
            
            # Save the extracted data
            with open(f"outputs/actual_{zp_company_id}.json", "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            
            print(f"✅ Successfully extracted data for {zp_company_id}")
            return  # Success
                
        except Exception as e:
            # If UDU parsing fails, save the raw output
            error_data = {
                "status": "error",
                "error": "UDU parsing failed",
                "raw_output": udu_output,
                "error_details": str(e)
            }
            with open(f"outputs/actual_{zp_company_id}.json", "w", encoding="utf-8") as f:
                json.dump(error_data, f, ensure_ascii=False, indent=2)
            print(f"⚠️ UDU parsing failed, saved error data for {zp_company_id}")
            return  # Exit even if parsing failed
            
    except Exception as e:
        print(f"Extract error: {e}")
        raise e


def process_transaction(company_data: dict):
    """Process a single transaction from the CSV file with two-stage approach."""
    zp_company_id = company_data['ZP Company ID']
    print(f"\nProcessing transaction for: {zp_company_id}")
    
    # Get start time
    start_time = time.time()
    
    try:
        # Stage 1: Search for company using separate agent instance
        print(f"Stage 1: Searching for company {zp_company_id}...")
        search_success, search_data, screenshot_base64 = search_company(company_data)
        
        print(f"Search result: success={search_success}, data={search_data}")
        
        if not search_success:
            # Log failure and stop processing
            log_failure(zp_company_id, "Search failed - company not found")
            print(f"Search failed for {zp_company_id}. Stopping processing.")
            
            # Calculate time taken
            end_time = time.time()
            time_taken = end_time - start_time
            print(f"Time taken for company ID {zp_company_id}: {time_taken} seconds")
            
            # Write to csv with failed status
            write_to_csv(time_taken, company_data['Company Name'], "Failed")
            return
        
        # Stage 2: Extract company information using UDU with separate agent instance
        print(f"Stage 2: Extracting information for company {zp_company_id} using UDU...")
        
        # Add timeout for extract stage
        import signal
        
        def timeout_handler(signum, frame):
            raise TimeoutError("Extract stage timed out")
        
        # Set timeout to 30 seconds for extract stage
        signal.signal(signal.SIGALRM, timeout_handler)
        signal.alarm(30)
        
        try:
            extract_company_info_with_udu(company_data, screenshot_base64)
            signal.alarm(0)  # Cancel timeout
        except TimeoutError:
            print(f"Extract stage timed out for {zp_company_id}")
            log_failure(zp_company_id, "Extract stage timed out")
            write_to_csv(time.time() - start_time, company_data['Company Name'], "Timeout")
            return
        except Exception as e:
            signal.alarm(0)  # Cancel timeout
            raise e
        
        # Calculate time taken
        end_time = time.time()
        time_taken = end_time - start_time
        print(f"Time taken for company ID {zp_company_id}: {time_taken} seconds")
        
        # Write to csv with success status
        write_to_csv(time_taken, company_data['Company Name'], "Successful")
        
    except Exception as e:
        # Log any unexpected errors
        log_failure(zp_company_id, f"Unexpected error: {str(e)}")
        print(f"Error processing {zp_company_id}: {e}")
        
        # Calculate time taken
        end_time = time.time()
        time_taken = end_time - start_time
        print(f"Time taken for company ID {zp_company_id}: {time_taken} seconds")
        
        # Write to csv with error status
        write_to_csv(time_taken, company_data['Company Name'], "Error")


def main():
    """Run the CUA (Computer Use Assistant) loop, processing transactions from CSV."""
    # Create outputs directory if it doesn't exist
    os.makedirs("outputs", exist_ok=True)
    os.makedirs("outputs/api_calls", exist_ok=True)

    # Remove all files in the outputs directory
    for file in glob.glob("outputs/api_calls/*.json"):
        os.remove(file)

    # Read transactions from CSV
    with open('input/cleaned_transactions.csv', 'r') as f:
        reader = csv.DictReader(f)
        transactions = list(reader)

    # Process each transaction
    for i, transaction in enumerate(transactions):
        print(f"\n{'='*50}")
        print(f"Processing transaction {i+1}/{len(transactions)}")
        print(f"{'='*50}")
        
        try:
            process_transaction(transaction)
        except Exception as e:
            print(f"Error processing transaction {i+1}: {e}")
            log_failure(transaction['ZP Company ID'], f"Main process error: {e}")
        
        # No delay between transactions - test if browser closes properly
        

if __name__ == "__main__":
    main()
