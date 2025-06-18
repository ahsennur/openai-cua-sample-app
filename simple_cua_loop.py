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
    csv_file = "response_times.csv"
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
        #prompt = modifiable_prompt + "\n\n" + static_prompt_template
        prompt = modifiable_prompt
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
                raise ValueError("No output from model")

            # Add the response to the items to maintain context & compliance
            items += response["output"]

            # Handle each item
            for item in response["output"]:
                items += handle_item(item, computer)

            # Check if the last item is an assistant response
            if items[-1].get("role") == "assistant":
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
