import streamlit as st
import pdfplumber
import os
import time
import requests
import smtplib
from datetime import datetime, timedelta
from email.message import EmailMessage
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from openai import OpenAI
import openai

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
    accident_description: str = Field(description="A brief, warm 1-sentence summary of the incident for the client.")


def generate_warm_email(client_name, verified_date, verified_desc):
    prompt = f"""
    Write a warm, empathetic professional email from Andrew Richards at Richards & Law.
    
    Context:
    - Client Name: {client_name}
    - Date of Incident: {verified_date}
    - Description of Incident: {verified_desc}
    - Call to Action: Review the attached Retainer Agreement.

    Guidelines:
    - Tone: Supportive, grounded, and advocate-focused.
    - Acknowledge the stress of the situation (e.g., "aftermath of a crash is stressful").
    - Briefly summarize their side of the story to show we are listening.
    - Keep it professional but skip the "legal-speak" where possible.
    - IMPORTANT: Do NOT include any placeholders, URLs, website links, or scheduling links in the email. Keep it strictly text-based.
    - FORMATTING REQUIREMENT: Start your response with the subject line formatted exactly as "Subject: [Your Subject Here]". Leave a blank line, then write the email body.
    """

    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an empathetic personal injury attorney writing to a new client."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.7
    )
    
    raw_response = response.choices[0].message.content.strip()
    
    subject = "Next Steps: Your Case & Retainer Agreement" 
    body = raw_response
    
    if raw_response.lower().startswith("subject:"):
        parts = raw_response.split("\n", 1) 
        subject = parts[0][8:].strip()      
        body = parts[1].strip() if len(parts) > 1 else ""
        
    return subject, body


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


# Initialize session state for email review workflow
if "email_stage" not in st.session_state:
    st.session_state.email_stage = False

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
    
    # --- CALCULATE EXACT DATES IN PYTHON ---
    today = datetime.now()
    try:
        eight_years_from_now = today.replace(year=today.year + 8)
    except ValueError:
        # Handles the rare case where today is Feb 29 (leap year)
        eight_years_from_now = today.replace(year=today.year + 8, month=2, day=28)
        
    calculated_sol_date = eight_years_from_now.strftime("%Y-%m-%d")
    calculated_follow_up_date = (today + timedelta(days=7)).strftime("%Y-%m-%d")

    # --- 4. HUMAN VERIFICATION UI ---
    st.subheader("Data Verification")
    st.info("Review AI-extracted data before pushing to Clio.")
    
    col1, col2 = st.columns(2)
    with col1:
        verified_date = st.text_input("Accident Date", value=data.accident_date)
        verified_location = st.text_input("Accident Location", value=data.accident_location)
        verified_sol = st.text_input("Statute of Limitations (8 Yrs from Today)", value=calculated_sol_date)
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
                            (483287, verified_injuries)
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

                    # --- Step B: Trigger Document Automation (THE ROUTER) ---
                    safe_matter_name = selected_matter.get('display_number', 'Matter').replace(" ", "_")
                    timestamp = datetime.now().strftime("%H%M%S")
                    expected_filename = f"Retainer_{safe_matter_name}_{timestamp}"

                    with st.spinner("Selecting correct template and generating Retainer Agreement..."):
                        
                        if verified_injuries:
                            TARGET_TEMPLATE_ID = 360005  # Injured Template
                        else:
                            TARGET_TEMPLATE_ID = 360002  # No Injuries Template
                        
                        doc_url = "https://eu.app.clio.com/api/v4/document_automations.json"
                        doc_payload = {
                            "data": {
                                "matter": {"id": selected_matter["id"]},
                                "document_template": {"id": TARGET_TEMPLATE_ID}, 
                                "filename": expected_filename,
                                "formats": ["pdf"]
                            }
                        }    
                        
                        doc_res = requests.post(doc_url, headers=headers, json=doc_payload)
                        
                        if doc_res.status_code in [200, 201]:
                            st.success("📄 Retainer Agreement generation triggered using dynamic template logic!")
                        else:
                            st.error("Matter updated, but Document Generation failed.")
                            st.stop()

                    # --- Step C: Calendar the SOL and 1-Week Follow Up ---
                    with st.spinner("Setting up Calendar Reminders..."):
                        matter_details_url = f"https://eu.app.clio.com/api/v4/matters/{selected_matter['id']}.json?fields=id,responsible_attorney{{id,name}}"
                        attorney_res = requests.get(matter_details_url, headers=headers)
                        
                        if attorney_res.status_code == 200:
                            attorney_data = attorney_res.json().get("data", {})
                            responsible_attorney = attorney_data.get("responsible_attorney")
                            
                            if responsible_attorney:
                                attorney_name = responsible_attorney.get("name")
                                cal_url = "https://eu.app.clio.com/api/v4/calendars.json?fields=id,name"
                                cal_res = requests.get(cal_url, headers=headers)
                                
                                calendar_id = None
                                if cal_res.status_code == 200:
                                    calendars = cal_res.json().get("data", [])
                                    for cal in calendars:
                                        if cal.get("name") == attorney_name:
                                            calendar_id = cal["id"]
                                            break
                                    if not calendar_id and calendars:
                                        calendar_id = calendars[0]["id"]
                                
                                if calendar_id:
                                    calendar_url = "https://eu.app.clio.com/api/v4/calendar_entries.json"
                                    
                                    # 1. SOL Calendar Entry
                                    sol_start_time = f"{verified_sol}T09:00:00Z"
                                    sol_end_time = f"{verified_sol}T10:00:00Z"
                                    sol_payload = {
                                        "data": {
                                            "summary": f"Statute of Limitations - {verified_at_fault}",
                                            "description": f"8-Year SOL Deadline. Accident Description: {verified_desc}",
                                            "start_at": sol_start_time,
                                            "end_at": sol_end_time,
                                            "matter": {"id": selected_matter["id"]},
                                            "calendar_owner": {"id": calendar_id, "type": "UserCalendar"}
                                        }
                                    }
                                    sol_res = requests.post(calendar_url, headers=headers, json=sol_payload)
                                    if sol_res.status_code in [200, 201]:
                                        st.success(f"🗓️ Statute of Limitations successfully calendared for **{verified_sol}**!")
                                    else:
                                        st.error("Failed to create SOL Calendar Entry.")
                                        
                                    # 2. 1-Week Follow-Up Entry
                                    fu_start_time = f"{calculated_follow_up_date}T10:00:00Z"
                                    fu_end_time = f"{calculated_follow_up_date}T10:30:00Z"
                                    fu_payload = {
                                        "data": {
                                            "summary": f"Follow-Up: Retainer Agreement - {verified_at_fault}",
                                            "description": f"Check if the client has signed the retainer agreement sent on {today.strftime('%Y-%m-%d')}.",
                                            "start_at": fu_start_time,
                                            "end_at": fu_end_time,
                                            "matter": {"id": selected_matter["id"]},
                                            "calendar_owner": {"id": calendar_id, "type": "UserCalendar"}
                                        }
                                    }
                                    fu_res = requests.post(calendar_url, headers=headers, json=fu_payload)
                                    if fu_res.status_code in [200, 201]:
                                        st.success(f"🔔 Follow-Up Reminder successfully calendared for **{calculated_follow_up_date}**!")
                                        st.info(f"📍 **Clio Location:** Both events added to **{attorney_name}'s calendar** under Matter **{selected_matter.get('display_number', 'N/A')}**.")
                                    else:
                                        st.error("Failed to create Follow-Up Calendar Entry.")

                                else:
                                    st.warning("⚠️ Could not find a matching calendar for the Responsible Attorney.")
                            else:
                                st.warning("⚠️ No Responsible Attorney assigned to this matter in Clio. Skipping calendaring.")

                    # --- Step D & E: Poll for Document and Generate Draft ---
                    with st.spinner("Waiting for Document to finish generating..."):
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
                            st.error("Timed out waiting for document to appear in the Matter. Cannot prepare email.")
                            st.stop()

                        download_url = f"https://eu.app.clio.com/api/v4/documents/{document_id}/download"
                        pdf_res = requests.get(download_url, headers=headers)
                        if pdf_res.status_code != 200:
                            st.error("Failed to download generated document from Clio.")
                            st.stop()
                        
                        # Store properties in session state so we can show the review UI
                        st.session_state.pdf_bytes = pdf_res.content
                        st.session_state.expected_filename = expected_filename
                        
                        # Get parsed Subject and Body
                        draft_subject, draft_body = generate_warm_email(
                            client_name="Guillermo",
                            verified_date=verified_date, 
                            verified_desc=verified_desc
                        )
                        st.session_state.draft_subject = draft_subject
                        st.session_state.draft_body = draft_body
                        
                        st.session_state.email_stage = True
                        st.rerun()

        except Exception as e:
            st.error(f"An unexpected error occurred: {e}")

# --- 6. EMAIL REVIEW UI ---
if st.session_state.get("email_stage"):
    st.divider()
    st.subheader("✉️ Email Review")
    st.info("Review and edit the AI-generated email below before sending it to the client. The Retainer Agreement is queued and will be automatically attached.")
    
    with st.form("email_form"):
        # Let the user edit both the subject and the body
        edited_subject = st.text_input("Email Subject", value=st.session_state.draft_subject)
        edited_email = st.text_area("Draft Email Body", value=st.session_state.draft_body, height=300)
        
        submitted = st.form_submit_button("Confirm & Send Email")
        
        if submitted:
            with st.spinner("Sending email..."):
                msg = EmailMessage()
                msg['Subject'] = edited_subject
                msg['From'] = os.getenv("SMTP_USER") 
                msg['To'] = "19774907@sun.ac.za" 
                msg.set_content(edited_email)
                msg.add_attachment(
                    st.session_state.pdf_bytes, 
                    maintype='application', 
                    subtype='pdf', 
                    filename=f"{st.session_state.expected_filename}.pdf"
                )

                try:
                    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
                    smtp_port = int(os.getenv("SMTP_PORT", 587))
                    smtp_user = os.getenv("SMTP_USER")
                    smtp_pass = os.getenv("SMTP_PASSWORD")

                    with smtplib.SMTP(smtp_server, smtp_port) as server:
                        server.starttls()
                        server.login(smtp_user, smtp_pass)
                        server.send_message(msg)
                    
                    st.success("✅ Client Email sent successfully with the Retainer Agreement attached!")
                    st.balloons()
                    
                    # Reset the email stage so it doesn't get stuck open for the next file
                    st.session_state.email_stage = False
                    
                except Exception as e:
                    st.error(f"Failed to send email: {e}")
                    st.info("Check your SMTP credentials in the .env file.")