import streamlit as st
import pdfplumber
import os
import time
import requests
import smtplib
from datetime import datetime
from email.message import EmailMessage
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from openai import OpenAI

# Load environment variables
load_dotenv()
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Set up the page
st.set_page_config(page_title="Richards & Law - Intake Automation", layout="wide")

# --- 1. DEFINE THE DATA STRUCTURE ---
class CaseDetails(BaseModel):
    accident_date: str = Field(description="The date the accident occurred, formatted as YYYY-MM-DD.")
    at_fault_party: str = Field(description="The full name of the at-fault party or defendant.")
    accident_location: str = Field(description="The street, intersection, or address where the accident happened.")
    client_plate_number: str = Field(description="The license plate number of the client's vehicle (usually vehicle 1).")
    injuries_reported: bool = Field(description="True if any injuries were reported in the accident, False otherwise.")
    statute_of_limitations: str = Field(description="Exactly 8 years after the accident date, formatted as YYYY-MM-DD.")
    accident_description: str = Field(description="A brief, warm 1-sentence summary of the incident for the client.")

# --- 2. EXTRACTION FUNCTION ---
def extract_case_info(pdf_text):
    prompt = f"""
    You are an expert legal assistant. Read the following raw text extracted from a messy police report.
    Extract the critical accident details required for a retainer agreement.
    
    Raw Police Report Text:
    {pdf_text}
    """
    response = client.beta.chat.completions.parse(
        model="gpt-4o-mini",
        messages=[
            {"role": "system", "content": "You perfectly extract structured data from messy documents."},
            {"role": "user", "content": prompt}
        ],
        response_format=CaseDetails,
    )
    return response.choices[0].message.parsed

# --- 3. STREAMLIT UI ---
st.title("⚖️ Richards & Law: Police Report Intake")
st.markdown("Upload a police report to automatically extract data and update Clio Manage.")

uploaded_file = st.file_uploader("Upload Police Report (PDF)", type="pdf")

if uploaded_file is not None:
    with pdfplumber.open(uploaded_file) as pdf:
        raw_text = ""
        for page in pdf.pages:
            raw_text += page.extract_text() + "\n"
            
    st.success("PDF read successfully!")
    
    with st.spinner("AI is analyzing the police report..."):
        if "extracted_data" not in st.session_state:
            st.session_state.extracted_data = extract_case_info(raw_text)
            
    data = st.session_state.extracted_data
    
    st.divider()
    
    # --- 4. HUMAN VERIFICATION UI ---
    st.subheader("Data Verification")
    st.info("Review AI-extracted data before pushing to Clio.")
    
    col1, col2 = st.columns(2)
    with col1:
        verified_date = st.text_input("Accident Date", value=data.accident_date)
        verified_location = st.text_input("Accident Location", value=data.accident_location)
        verified_sol = st.text_input("Statute of Limitations (8 Yrs)", value=data.statute_of_limitations)
        verified_injuries = st.checkbox("Injuries Reported?", value=data.injuries_reported)
    with col2:
        verified_at_fault = st.text_input("At-Fault Party", value=data.at_fault_party)
        verified_plate = st.text_input("Client Plate Number", value=data.client_plate_number)
        verified_desc = st.text_area("Email Description", value=data.accident_description)
    
    st.divider()
    
    # --- 5. MATTER SELECTION & PUSH UI ---
    st.subheader("Clio Matter Assignment")
    
    CLIO_TOKEN = os.getenv("CLIO_ACCESS_TOKEN")
    
    if not CLIO_TOKEN:
        st.error("Missing Clio Access Token. Please run your auth script.")
    else:
        headers = {
            "Authorization": f"Bearer {CLIO_TOKEN}",
            "Content-Type": "application/json"
        }
        
        matter_url = "https://eu.app.clio.com/api/v4/matters.json?fields=id,display_number,description"
        
        try:
            response = requests.get(matter_url, headers=headers)
            
            if response.status_code != 200:
                st.error(f"Clio API Error: {response.text}")
                matters = []
            else:
                matters = response.json().get("data", [])
            
            if not matters and response.status_code == 200:
                st.warning("No open matters found in your Clio account.")
            elif matters:
                matter_options = {f"{m.get('display_number', 'N/A')} - {m.get('description', 'No Desc')}": m for m in matters}
                selected_label = st.selectbox("Select the target Matter in Clio:", options=list(matter_options.keys()))
                selected_matter = matter_options[selected_label]
                
                if st.button("Verify & Push to Clio"):
                    
                    # --- Step A: Update Matter Custom Fields ---
                    with st.spinner(f"Pushing data to Clio Matter..."):
                        check_url = f"https://eu.app.clio.com/api/v4/matters/{selected_matter['id']}.json?fields=custom_field_values{{id,custom_field}}"
                        check_res = requests.get(check_url, headers=headers)
                        
                        existing_cfvs = []
                        if check_res.status_code == 200:
                            existing_cfvs = check_res.json().get("data", {}).get("custom_field_values", [])
                        
                        cfv_map = {cfv.get("custom_field", {}).get("id"): cfv["id"] for cfv in existing_cfvs if "custom_field" in cfv}
                                
                        new_cfvs = []
                        fields_to_update = [
                            (482522, verified_date),
                            (482525, verified_at_fault),
                            (482528, verified_desc),
                            (483278, verified_location),
                            (483281, verified_plate),
                            (483284, verified_sol),
                            (483287, str(verified_injuries).lower()) 
                        ]
                        
                        for cf_id, val in fields_to_update:
                            cf_payload = {"custom_field": {"id": cf_id}, "value": val}
                            if cf_id in cfv_map:
                                cf_payload["id"] = cfv_map[cf_id]
                            new_cfvs.append(cf_payload)
                            
                        update_url = f"https://eu.app.clio.com/api/v4/matters/{selected_matter['id']}.json"
                        payload = {"data": {"custom_field_values": new_cfvs}}
                        update_res = requests.patch(update_url, headers=headers, json=payload)
                        
                        if update_res.status_code != 200:
                            st.error("Failed to update the Matter.")
                            st.stop()
                        st.success(f"✅ Data pushed to Matter successfully!")

                    # --- Step B: Trigger Document Automation ---
                    safe_matter_name = selected_matter.get('display_number', 'Matter').replace(" ", "_")
                    expected_filename = f"Retainer_Agreement_{safe_matter_name}"
                    
                    with st.spinner("Generating Retainer Agreement PDF..."):
                        doc_url = "https://eu.app.clio.com/api/v4/document_automations.json"
                        doc_payload = {
                            "data": {
                                "matter": {"id": selected_matter["id"]},
                                "document_template": {"id": 359795}, # Ensure this matches your template ID!
                                "filename": expected_filename,
                                "formats": ["pdf"]
                            }
                        }
                        
                        doc_res = requests.post(doc_url, headers=headers, json=doc_payload)
                        
                        if doc_res.status_code in [200, 201]:
                            st.success("📄 Retainer Agreement generation triggered!")
                        else:
                            st.error("Matter updated, but Document Generation failed.")
                            st.stop()

                    # --- Step C: Calendar the Statute of Limitations ---
                    with st.spinner("Calendaring the Statute of Limitations..."):
                        # Get attorney details to match calendar name
                        matter_details_url = f"https://eu.app.clio.com/api/v4/matters/{selected_matter['id']}.json?fields=id,responsible_attorney{{id,name}}"
                        attorney_res = requests.get(matter_details_url, headers=headers)
                        
                        if attorney_res.status_code == 200:
                            attorney_data = attorney_res.json().get("data", {})
                            responsible_attorney = attorney_data.get("responsible_attorney")
                            
                            if responsible_attorney:
                                attorney_name = responsible_attorney.get("name")
                                
                                # Fetch calendars to get the UserCalendar ID
                                cal_url = "https://eu.app.clio.com/api/v4/calendars.json?fields=id,name"
                                cal_res = requests.get(cal_url, headers=headers)
                                
                                calendar_id = None
                                if cal_res.status_code == 200:
                                    calendars = cal_res.json().get("data", [])
                                    # Try to match the attorney's name, else fallback to the first calendar
                                    for cal in calendars:
                                        if cal.get("name") == attorney_name:
                                            calendar_id = cal["id"]
                                            break
                                    if not calendar_id and calendars:
                                        calendar_id = calendars[0]["id"]
                                
                                if calendar_id:
                                    calendar_url = "https://eu.app.clio.com/api/v4/calendar_entries.json"
                                    start_time = f"{verified_sol}T09:00:00Z"
                                    end_time = f"{verified_sol}T10:00:00Z"
                                    
                                    calendar_payload = {
                                        "data": {
                                            "summary": f"Statute of Limitations - {verified_at_fault}",
                                            "description": f"8-Year SOL Deadline. Accident Description: {verified_desc}",
                                            "start_at": start_time,
                                            "end_at": end_time,
                                            "matter": {"id": selected_matter["id"]},
                                            # The Reverse-Engineered Secret Sauce
                                            "calendar_owner": {"id": calendar_id, "type": "UserCalendar"}
                                        }
                                    }
                                    cal_res = requests.post(calendar_url, headers=headers, json=calendar_payload)
                                    if cal_res.status_code in [200, 201]:
                                        st.success("🗓️ Statute of Limitations successfully calendared!")
                                    else:
                                        st.error("Failed to create Calendar Entry.")
                                        st.json(cal_res.json())
                                else:
                                    st.warning("⚠️ Could not find a matching calendar for the Responsible Attorney.")
                            else:
                                st.warning("⚠️ No Responsible Attorney assigned to this matter in Clio. Skipping calendaring.")

                    # --- Step D & E: Poll for Document, Download, and Email ---
                    with st.spinner("Waiting for Document, preparing Email..."):
                        current_month = datetime.now().month
                        if 3 <= current_month <= 8:
                            scheduling_link = "https://richards-law.com/in-office-scheduling"
                        else:
                            scheduling_link = "https://richards-law.com/virtual-scheduling"

                        document_id = None
                        docs_url = f"https://eu.app.clio.com/api/v4/documents.json?matter_id={selected_matter['id']}&fields=id,name,filename"
                        
                        for _ in range(15): 
                            time.sleep(2)
                            docs_res = requests.get(docs_url, headers=headers)
                            
                            if docs_res.status_code == 200:
                                docs_data = docs_res.json().get("data", [])
                                
                                for doc in docs_data:
                                    doc_name = doc.get("filename") or doc.get("name") or ""
                                    if expected_filename in doc_name:
                                        document_id = doc["id"]
                                        break
                                
                                if document_id:
                                    break
                        
                        if not document_id:
                            st.error("Timed out waiting for document to appear in the Matter. Cannot send email.")
                            st.stop()

                        download_url = f"https://eu.app.clio.com/api/v4/documents/{document_id}/download"
                        pdf_res = requests.get(download_url, headers=headers)
                        if pdf_res.status_code != 200:
                            st.error("Failed to download generated document from Clio.")
                            st.stop()
                        
                        pdf_bytes = pdf_res.content

                        msg = EmailMessage()
                        msg['Subject'] = "Next Steps: Your Case & Retainer Agreement"
                        msg['From'] = os.getenv("SMTP_USER") 
                        
                        # --- TESTING EMAIL ADDRESS ---
                        msg['To'] = "19774907@sun.ac.za" 
                        
                        email_body = f"""Hello,

Thank you for reaching out to Richards & Law. We have reviewed the details regarding your recent incident on {verified_date}: {verified_desc}. We are deeply sorry you had to go through this, but we are here to help.

To move forward, we have prepared your Retainer Agreement, which is attached to this email as a PDF for your review.

Please book a consultation with us at your earliest convenience using the link below:
{scheduling_link}

Best regards,
Andrew Richards
Richards & Law
"""
                        msg.set_content(email_body)
                        msg.add_attachment(pdf_bytes, maintype='application', subtype='pdf', filename=f"{expected_filename}.pdf")

                        try:
                            smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
                            smtp_port = int(os.getenv("SMTP_PORT", 587))
                            smtp_user = os.getenv("SMTP_USER")
                            smtp_pass = os.getenv("SMTP_PASSWORD")

                            with smtplib.SMTP(smtp_server, smtp_port) as server:
                                server.starttls()
                                server.login(smtp_user, smtp_pass)
                                server.send_message(msg)
                            st.success("✉️ Client Email sent successfully with the Retainer Agreement attached!")
                            st.balloons()
                        except Exception as e:
                            st.error(f"Failed to send email: {e}")
                            st.info("Check your SMTP credentials in the .env file.")
        except Exception as e:
            st.error(f"An unexpected error occurred: {e}")