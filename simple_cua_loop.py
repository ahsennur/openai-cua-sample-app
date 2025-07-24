"""A script that runs a Computer Use Assistant (CUA) loop using Local Playwright."""

import os
import glob
import time
import csv
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


def process_transaction(company_data: dict):
    """Process a single transaction from the CSV file."""
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

        # Read and process the prompt template
        fein = company_data['ZP FEIN']
        state_ein = company_data['ZP State EIN']

        with open("input/modifiable_user_input.txt", "r") as f:
            modifiable_prompt_template = f.read()
            modifiable_prompt = modifiable_prompt_template.format(
                fein=fein,
                state_ein=state_ein
            )
        
        with open("input/static_user_input.txt", "r") as f:
            static_prompt_template = f.read()

        # Append the static prompt to the modifiable prompt
        prompt = modifiable_prompt + "\n\n" + static_prompt_template
        items.append({"role": "user", "content": prompt})

        # Get start time
        start_time = time.time()

        # keep looping until we get a final response
        while True:
            response = create_response(
                model="computer-use-preview",
                input=items,
                tools=tools,
                truncation="auto",
                index=i,
                include=["reasoning.encrypted_content"]
            )

            # Check if the response is valid
            if "output" not in response:
                print(response)
                error_type = response.get('error', {}).get('type', '')
                error_message = response.get('error', {}).get('message', '')
                if error_type:
                    raise ValueError(f"No output from model. API error: {error_type}: {error_message}")
                else:
                    raise ValueError(f"No output from model. API error: {error_message}")

            # Add the response to the items to maintain context & compliance
            items += response["output"]

            # Handle each item
            for item in response["output"]:
                items += handle_item(item, computer)

            # Check if the last item is an assistant response
            if items[-1].get("role") == "assistant":
                # Save the assistant's JSON output to a file for this transaction
                assistant_output = items[-1]["content"][0]["text"]
                zp_company_id = company_data['ZP Company ID']
                import json
                try:
                    # Parse the assistant's output as JSON
                    data = json.loads(assistant_output.replace("'", '"'))
                    # If 'sui_rate' exists, convert to float and divide by 100
                    if "sui_rate" in data:
                        try:
                            rate = str(data["sui_rate"]).replace("%", "").strip()
                            data["sui_rate"] = float(rate) / 100
                        except Exception:
                            pass
                    # If 'state_ein' exists, remove all non-digit characters
                    if "state_ein" in data:
                        import re
                        data["state_ein"] = re.sub(r"\D", "", str(data["state_ein"]))
                    # If 'fein' exists, remove all non-digit characters
                    if "fein" in data:
                        import re
                        data["fein"] = re.sub(r"\D", "", str(data["fein"]))
                    # Save the possibly modified JSON
                    with open(f"outputs/actual_{zp_company_id}.json", "w", encoding="utf-8") as f:
                        json.dump(data, f, ensure_ascii=False, indent=2)
                except Exception:
                    # If parsing fails, save as is
                    with open(f"outputs/actual_{zp_company_id}.json", "w", encoding="utf-8") as f:
                        f.write(assistant_output)
                break

            i += 1

        # Print the time taken for the response
        end_time = time.time()
        time_taken = end_time - start_time
        print(f"Time taken for company ID {company_data['ZP Company ID']}: {time_taken} seconds")

        # write to csv
        write_to_csv(time_taken, company_data['Company Name'], company_data['Status'])


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
    for transaction in transactions:
        print(f"\nProcessing transaction for: {transaction['ZP Company ID']}")
        process_transaction(transaction)


if __name__ == "__main__":
    main()
