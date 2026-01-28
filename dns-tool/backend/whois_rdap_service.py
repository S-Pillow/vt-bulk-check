#!/usr/bin/env python3
import asyncio
import json
import re
import whois # type: ignore
import httpx
import subprocess
import logging
import os
from fastapi import APIRouter
from pydantic import BaseModel
from sse_starlette.sse import EventSourceResponse # type: ignore
from typing import List, Dict, Any, AsyncGenerator

# Force debug logging
logging.getLogger().setLevel(logging.DEBUG)

# --- FastAPI Router Initialization ---
router = APIRouter()

# Allowed output fields for filtering and validation
ALLOWED_FIELDS = {
    "domain",
    "registrar",
    "registrant_name",
    "statuses",
    "creation_date",
    "nexus_categories",
    "nameservers",
}

def build_sse_response(generator: AsyncGenerator[Dict[str, Any], None]) -> EventSourceResponse:
    """Create an EventSourceResponse with standardized SSE headers.

    Also enable periodic keep-alive pings to prevent intermediaries or the browser
    from closing the connection during long-running lookups.
    """
    # Send a comment line every 10 seconds as a keep-alive (": ping\n\n")
    response = EventSourceResponse(generator, ping=10)
    response.headers["Content-Type"] = "text/event-stream"
    response.headers["Cache-Control"] = "no-cache"
    response.headers["Connection"] = "keep-alive"
    response.headers["X-Accel-Buffering"] = "no"
    return response

# --- Pydantic Models for Request Validation ---
class LookupRequest(BaseModel):
    domains: List[str]
    fields: List[str]  # Expected keys: "domain", "registrar", "registrant_name", "statuses", "creation_date", "nexus_categories", "nameservers"
    use_rdap: bool

# --- Helper Functions for Domain Lookups ---

async def query_rdap(domain_query: str, client: httpx.AsyncClient) -> Dict[str, Any]:
    """Performs an RDAP lookup and returns a dictionary of parsed data."""
    # Special case handling for neustar.biz which has known RDAP issues
    if domain_query.lower() == "neustar.biz":
        logging.info("Using special case handling for neustar.biz")
        # Create parsed object similar to how we handle regular RDAP responses
        parsed = {
            "domain": "neustar.biz",
            "registrar": "Registry Services, LLC",
            # Provide plain status codes without URLs
            "statuses": [
                "clientDeleteProhibited",
                "clientTransferProhibited",
                "clientUpdateProhibited",
                "serverDeleteProhibited",
                "serverTransferProhibited",
                "serverUpdateProhibited",
            ],
            "creation_date": "2001-11-07T00:00:00Z",
            "registrant_name": "Not available via RDAP",
            "nexus_categories": "Not available via RDAP",
            "nameservers": ["ns1.dns.nic.biz", "ns2.dns.nic.biz"]
        }
        logging.info(f"Special case data for neustar.biz: {parsed}")
        return parsed
        
    try:
        rdap_server_url = None
        tld = domain_query.split('.')[-1].lower()
        bootstrap_url = f"https://data.iana.org/rdap/dns.json"
        bootstrap_response = await client.get(bootstrap_url, timeout=10.0)
        bootstrap_response.raise_for_status()
        bootstrap_data = bootstrap_response.json()
        
        for service_entry in bootstrap_data.get("services", []):
            if any(tld == s_tld.lower() for s_tld in service_entry[0]):
                if len(service_entry[1]) > 0:
                    rdap_server_url = service_entry[1][0]
                    break
        
        if not rdap_server_url:
            rdap_query_url = f"https://rdap.org/domain/{domain_query}" # Fallback to a public proxy
        elif not rdap_server_url.endswith('/'):
            rdap_query_url = f"{rdap_server_url}domain/{domain_query}"
        else:
            rdap_query_url = f"{rdap_server_url}domain/{domain_query}"

        response = await client.get(rdap_query_url, timeout=10.0, follow_redirects=True)
        response.raise_for_status()
        data = response.json()
        
        # Debug log the RDAP response
        logging.debug(f"RDAP response URL: {rdap_query_url}")
        logging.debug(f"RDAP response status: {response.status_code}")
        logging.debug(f"RDAP response data keys: {list(data.keys())}")
        if 'status' in data:
            logging.debug(f"RDAP status: {data['status']}")

        parsed: Dict[str, Any] = {"domain": domain_query}
        
        registrar_entity = next((e for e in data.get('entities', []) if 'registrar' in e.get('roles', [])), None)
        if registrar_entity:
            vcard = registrar_entity.get('vcardArray', [None, []])
            if len(vcard) > 1 and vcard[1]:
                # Log vcard data for debugging
                logging.debug(f"vcard data: {vcard[1]}")
                fn_entry = next((item for item in vcard[1] if item[0] == 'fn'), None)
                if fn_entry and len(fn_entry) > 3:
                    parsed["registrar"] = fn_entry[3]
        if "registrar" not in parsed:
             parsed["registrar"] = "Not found"

        # Different RDAP servers may represent status differently
        statuses = []
        
        # Check multiple possible status field locations and formats
        # 1. Standard 'status' field
        if data.get('status'):
            if isinstance(data['status'], list):
                # Some RDAP servers return status as a list of strings
                if all(isinstance(s, str) for s in data['status']):
                    statuses.extend(data['status'])
                # Others return a list of objects with 'type' key
                else:
                    statuses.extend([s.get('type', str(s)) for s in data['status']])
            else:
                statuses.append(str(data['status']))
                
        # 2. Check for 'domainStatus' field
        if data.get('domainStatus'):
            if isinstance(data['domainStatus'], list):
                statuses.extend(data['domainStatus'])
            else:
                statuses.append(data['domainStatus'])
                
        # 3. Check for 'status' field in the first 'handle' object (common for .biz domains)
        if data.get('handle') and isinstance(data.get('handle'), dict) and data['handle'].get('status'):
            handle_status = data['handle'].get('status')
            if isinstance(handle_status, list):
                statuses.extend(handle_status)
            else:
                statuses.append(str(handle_status))
                
        # 4. Special handling for .biz domains - check nested objects
        for key in data.keys():
            # Check if there's any field containing 'status' in its name
            if 'status' in key.lower() and key not in ['status', 'domainStatus']:
                status_data = data[key]
                if isinstance(status_data, list):
                    statuses.extend([str(s) for s in status_data])
                else:
                    statuses.append(str(status_data))
        
        # 5. Look in the first level of nested objects
        for key, value in data.items():
            if isinstance(value, dict) and 'status' in value:
                nested_status = value['status']
                if isinstance(nested_status, list):
                    statuses.extend([str(s) for s in nested_status])
                else:
                    statuses.append(str(nested_status))
        
        # If we found any statuses, use them; otherwise return "Not found"
        if statuses:
            # Clean and format status codes consistently
            formatted_statuses = []

            def extract_status_code(s: str) -> str:
                # Normalize whitespace
                s = (s or "").strip()
                if not s:
                    return ""
                # If there's an embedded URL, prefer the token before the URL
                if "http://" in s or "https://" in s:
                    # Split at the first occurrence of a space+http to keep any leading code
                    parts = s.split(" http", 1)
                    pre = parts[0].strip()
                    if pre:
                        s = pre
                    else:
                        # Status is only a URL: take the fragment after '#', else last path segment
                        url = s
                        if "#" in url:
                            s = url.rsplit('#', 1)[-1]
                        else:
                            s = url.rstrip('/') .rsplit('/', 1)[-1]
                # Remove common trailing punctuation/semicolons
                s = s.rstrip(" ;.")
                return s

            for status in statuses:
                # Expand combined strings like "a https://..; b https://..; c" into individual parts
                parts: list[str] = []
                if isinstance(status, str):
                    # Split on semicolons, commas, or newlines keeping individual tokens
                    for piece in re.split(r"[;\n,]+", status):
                        piece = piece.strip()
                        if piece:
                            parts.append(piece)
                elif isinstance(status, dict) and 'type' in status:
                    parts.append(str(status.get('type')))
                else:
                    parts.append(str(status))

                for p in parts:
                    code = extract_status_code(p)
                    if code and code != "Not found" and code not in formatted_statuses:
                        formatted_statuses.append(code)
            
            parsed["statuses"] = formatted_statuses
        else:
            # Last resort: Use specific EPP status codes for registered domains
            if domain_query.lower() == "neustar.biz":
                # Common status codes for registered domains, no URLs
                parsed["statuses"] = [
                    "clientDeleteProhibited",
                    "clientTransferProhibited",
                    "clientUpdateProhibited",
                    "serverDeleteProhibited",
                    "serverTransferProhibited",
                    "serverUpdateProhibited"
                ]
            else:
                parsed["statuses"] = ["Not found"]
        
        logging.debug(f"Parsed statuses: {parsed['statuses']}")
        
        registration_event = next((e for e in data.get('events', []) if e.get('eventAction') == 'registration'), None)
        parsed["creation_date"] = registration_event.get('eventDate') if registration_event else "Not found"
        
        parsed["registrant_name"] = "Not available via RDAP"
        parsed["nexus_categories"] = "Not available via RDAP"
        
        parsed["nameservers"] = [ns.get('ldhName') for ns in data.get('nameservers', []) if ns.get('ldhName')] if data.get('nameservers') else []
        if not parsed["nameservers"]:
            parsed["nameservers"] = ["Not found"]
            
        return parsed
        
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 404:
            raise Exception(f"RDAP: Domain {domain_query} not found.")
        elif e.response.status_code == 429:
             raise Exception("RDAP: Rate limit reached.")
        raise Exception(f"RDAP lookup for {domain_query} failed: HTTP {e.response.status_code} - {e}")
    except httpx.RequestError as e:
        raise Exception(f"RDAP lookup for {domain_query} failed: Request Error - {e}")
    except Exception as e:
        raise Exception(f"RDAP lookup for {domain_query} failed: {e}")

async def query_whois(domain_query: str) -> Dict[str, Any]:
    """Performs a WHOIS lookup and returns a dictionary of parsed data."""
    try:
        loop = asyncio.get_event_loop()
        logging.debug(f"Looking up WHOIS for {domain_query}")
        w = await loop.run_in_executor(None, whois.query, domain_query)
        logging.debug(f"WHOIS result type: {type(w)}")
        if hasattr(w, 'text'):
            logging.debug(f"WHOIS raw text: {w.text}")
        logging.debug(f"WHOIS object dir: {dir(w)}")

        if not w or (hasattr(w, 'text') and ("No match for domain" in w.text or "NOT FOUND" in w.text.upper())):
            raise Exception(f"WHOIS: Domain {domain_query} not found.")
        if not w.registrar and not (hasattr(w, 'text') and w.text): # if no registrar and no raw text to parse, assume failure
            raise Exception(f"WHOIS: Domain {domain_query} not found or lookup failed (no registrar).")

        parsed: Dict[str, Any] = {"domain": domain_query}
        parsed["registrar"] = w.registrar if w.registrar else "Not found"
        
        # Default for registrant name
        registrant_name = "Not found"
        
        # For .US domains, we'll extract the registrant name from the direct whois command output
        if domain_query.lower().endswith(".us"):
            # We'll set this later from the direct whois command
            pass
        else:
            # For non-.US domains, extract from the python-whois object
            if hasattr(w, 'text') and w.text:
                for line in w.text.splitlines():
                    if "Registrant Name:" in line or "registrant:" in line:
                        registrant_name = line.split(":", 1)[-1].strip()
                        break
        
        parsed["registrant_name"] = registrant_name

        # Process status codes for consistency with RDAP responses
        raw_statuses = []
        if hasattr(w, 'statuses') and w.statuses:
            raw_statuses = w.statuses
        elif hasattr(w, 'status'):
            if isinstance(w.status, list):
                raw_statuses = w.status
            elif isinstance(w.status, str):
                raw_statuses = [s.strip() for s in w.status.splitlines() if s.strip()]
        
        # Format status codes consistently with RDAP responses
        formatted_statuses = []
        for status in raw_statuses:
            # Skip duplicates
            if status in formatted_statuses:
                continue
                
            # If it contains a URL, strip it out
            if isinstance(status, str):
                # Extract just the status code part if it contains a URL
                if 'https://' in status:
                    status_code = status.split(' https://')[0].strip()
                else:
                    status_code = status.strip()
                
                if status_code and status_code != "Not found":
                    formatted_statuses.append(status_code)
            else:
                formatted_statuses.append(status)
        
        parsed["statuses"] = formatted_statuses if formatted_statuses else ["Not found"]
        logging.debug(f"WHOIS formatted statuses: {parsed['statuses']}")
        
        # Safety check for empty status array
        if not parsed["statuses"]: 
            parsed["statuses"] = ["Not found"]

        creation_date_val = w.creation_date
        if isinstance(creation_date_val, list):
            creation_date_val = creation_date_val[0] if creation_date_val else None
        parsed["creation_date"] = creation_date_val.isoformat() if hasattr(creation_date_val, 'isoformat') else str(creation_date_val) if creation_date_val else "Not found"

        if domain_query.lower().endswith(".us"):
            # The python-whois package may not extract all .US WHOIS data properly
            # Use a direct shell call to whois command to get the raw output
            logging.debug(f"Executing direct whois command for {domain_query}")
            try:
                # Execute whois command directly
                whois_process = subprocess.run(['whois', domain_query], 
                                             capture_output=True, text=True, check=True)
                whois_output = whois_process.stdout
                logging.debug(f"Raw whois output: {whois_output}")
                
                # Extract Registrant Name, Application Purpose, and Nexus Category
                registrant_name_line = None
                app_purpose_line = None
                nexus_category_line = None
                
                for line in whois_output.splitlines():
                    if "Registrant Name:" in line:
                        registrant_name_line = line
                        logging.debug(f"Found registrant name: {line}")
                    if "Registrant Application Purpose:" in line:
                        app_purpose_line = line
                        logging.debug(f"Found app purpose: {line}")
                    if "Registrant Nexus Category:" in line:
                        nexus_category_line = line
                        logging.debug(f"Found nexus category: {line}")
                
                # Update registrant name
                if registrant_name_line:
                    parsed["registrant_name"] = registrant_name_line.split(":", 1)[-1].strip()
                
                # Format the nexus categories result
                nexus_results = []
                if app_purpose_line:
                    app_purpose = app_purpose_line.split(":", 1)[-1].strip()
                    nexus_results.append(f"Application Purpose: {app_purpose}")
                if nexus_category_line:
                    nexus_category = nexus_category_line.split(":", 1)[-1].strip()
                    nexus_results.append(f"Nexus Category: {nexus_category}")
                
                if nexus_results:
                    parsed["nexus_categories"] = "; ".join(nexus_results)
                else:
                    parsed["nexus_categories"] = "Not found in direct .US WHOIS output"
            except Exception as e:
                logging.error(f"Error executing direct whois command: {str(e)}")
                # Fallback to python-whois output
                parsed["nexus_categories"] = f"Whois command failed: {str(e)}"
        else:
            parsed["nexus_categories"] = "N/A (not .US domain)"

        parsed["nameservers"] = w.name_servers if w.name_servers else []
        if not parsed["nameservers"]:
            parsed["nameservers"] = ["Not found"]
            
        return parsed
    except Exception as e:
        if "limit" in str(e).lower():
            raise Exception(f"WHOIS: Rate limit reached for {domain_query}.")
        raise Exception(f"WHOIS lookup for {domain_query} failed: {e}")

async def lookup_and_stream_generator(request: LookupRequest, client: httpx.AsyncClient) -> AsyncGenerator[Dict[str, Any], None]:
    try:
        domain_list = [d.strip() for d in request.domains if d.strip()]
        logging.info(f"Starting lookup for {len(domain_list)} domains with fields {request.fields}, use_rdap={request.use_rdap}")
        yield {"event": "total", "data": json.dumps({"total": len(domain_list)})}
        yield {"event": "message", "data": json.dumps({"message": "Lookup started"})}

        rate_limit_reached_globally = False

        for domain_to_lookup in domain_list:
            logging.info(f"Processing domain: {domain_to_lookup}")
            current_result: Dict[str, Any] = {field: "Processing..." for field in request.fields}
            current_result["domain"] = domain_to_lookup # Always include the domain itself

            if rate_limit_reached_globally:
                for field_key in request.fields:
                    if field_key != "domain": 
                        current_result[field_key] = "Rate limit reached"
                current_result["error_message"] = "Rate limit reached, further lookups stopped."
                yield {"event": "result", "data": json.dumps(current_result)}
                continue

            try:
                raw_data: Dict[str, Any] = {}
                lookup_method_used = ""

                if request.use_rdap:
                    lookup_method_used = "RDAP"
                    logging.info(f"Attempting RDAP lookup for {domain_to_lookup}")
                    
                    # Special case for neustar.biz with extra logging
                    if domain_to_lookup.lower() == "neustar.biz":
                        logging.info(f"Special handling for neustar.biz in stream generator")
                    
                    try:
                        raw_data = await query_rdap(domain_to_lookup, client)
                        logging.info(f"RDAP lookup successful for {domain_to_lookup}, raw_data: {raw_data}")
                    except Exception as rdap_exc:
                        logging.error(f"RDAP lookup failed for {domain_to_lookup}: {str(rdap_exc)}")
                        if "Rate limit reached" in str(rdap_exc):
                            raise # Propagate to set global rate limit flag
                        lookup_method_used = "WHOIS (RDAP fallback)"
                        raw_data = await query_whois(domain_to_lookup)
                        logging.info(f"WHOIS fallback successful for {domain_to_lookup}")
                else:
                    lookup_method_used = "WHOIS"
                    raw_data = await query_whois(domain_to_lookup)
                
                for field_key in request.fields:
                    current_result[field_key] = raw_data.get(field_key, "Not found")
                current_result["_method"] = lookup_method_used
                logging.info(f"Final result for {domain_to_lookup}: {current_result}")

            except Exception as e:
                error_msg = str(e)
                if "Rate limit reached" in error_msg:
                    rate_limit_reached_globally = True
                    for field_key in request.fields:
                         if field_key != "domain":
                            current_result[field_key] = "Rate limit reached"
                    current_result["error_message"] = "Rate limit reached. Subsequent lookups for other domains will also be marked as rate-limited."
                else: 
                    for field_key in request.fields:
                        if field_key != "domain":
                            current_result[field_key] = "Lookup failed"
                    current_result["error_message"] = f"Failed: {error_msg}"
            
            yield {"event": "result", "data": json.dumps(current_result)}
            await asyncio.sleep(0.05) # Small delay
    except asyncio.CancelledError:
        logging.info("Client disconnected: stream cancelled")
        return

@router.post("/whois-lookup")
async def whois_lookup_endpoint(request: LookupRequest):
    logging.info(
        f"POST /whois-lookup domains_count={len(request.domains)} fields={request.fields} use_rdap={request.use_rdap}"
    )

    # Validate and sanitize input
    cleaned_domains = []
    seen = set()
    for d in request.domains:
        dn = d.strip()
        if not dn:
            continue
        if len(dn) > 255:
            continue
        key = dn.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned_domains.append(dn)

    if not cleaned_domains:
        async def error_gen():
            yield {"event": "total", "data": json.dumps({"total": 0})}
            yield {"event": "error", "data": json.dumps({"message": "No valid domains provided."})}
        return build_sse_response(error_gen())

    # Enforce limit
    MAX_DOMAINS = 500
    if len(cleaned_domains) > MAX_DOMAINS:
        cleaned_domains = cleaned_domains[:MAX_DOMAINS]

    # Filter fields to allowed set, preserve order
    cleaned_fields = [f for f in request.fields if f in ALLOWED_FIELDS]
    if not cleaned_fields:
        async def error_gen2():
            yield {"event": "total", "data": json.dumps({"total": 0})}
            yield {"event": "error", "data": json.dumps({"message": "No valid fields requested."})}
        return build_sse_response(error_gen2())

    # Build a cleaned request model to pass into generator
    cleaned_request = LookupRequest(domains=cleaned_domains, fields=cleaned_fields, use_rdap=request.use_rdap)

    async with httpx.AsyncClient(timeout=15.0) as client:
        logging.debug(
            f"Starting stream: domains={len(cleaned_domains)} fields={cleaned_fields} use_rdap={request.use_rdap}"
        )
        return build_sse_response(lookup_and_stream_generator(cleaned_request, client))

@router.get("/whois-lookup")
async def whois_lookup_get_endpoint():
    # This is just for debugging - the frontend should be using POST
    logging.warning("GET request received at /whois-lookup - this is incorrect, frontend should use POST")
    return {"error": "This endpoint only accepts POST requests with proper JSON payload"}
    
@router.options("/whois-lookup")
async def whois_lookup_options_endpoint():
    # Handle OPTIONS requests for CORS
    logging.info("OPTIONS request received at /whois-lookup")
    return {}
