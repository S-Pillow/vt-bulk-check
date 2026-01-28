#!/usr/bin/env python3
import os
import sys
import socket
import logging
import traceback
from datetime import datetime
from typing import Dict, List, Any, Optional

# --- Core FastAPI and DNS/URL Tool Imports ---
from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import dns.message
import dns.query
import dns.rdatatype
import dns.flags
import dns.exception
import dns.resolver
import dns.rcode

# --- Add current directory to path for local module imports ---
# This is crucial for running as a service where the working directory might be different.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# --- Import Local Routers and Utilities ---
from whois_rdap_service import router as whois_rdap_router
from routers.vt_bulk_check import router as vt_bulk_check_router
from utils.url_tools import sanitize_urls, unsanitize_urls, extract_domains
# from bulk_lookup import router as bulk_lookup_router # Temporarily disabled, file is empty

# --- Logging Configuration ---
# Ensure log directory exists before setting up FileHandler to prevent race conditions.
log_dir = "/var/log/dns-tool"
try:
    os.makedirs(log_dir, exist_ok=True)
    log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    logging.basicConfig(
        level=getattr(logging, log_level),
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        handlers=[
            logging.StreamHandler(sys.stdout),
            logging.FileHandler(os.path.join(log_dir, "api.log"), mode="a"),
        ],
    )
except Exception as e:
    # If logging fails, print to stderr and continue without file logging.
    print(f"Error setting up file logging: {e}", file=sys.stderr)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

logger = logging.getLogger("dns-tool")

# --- FastAPI Application Initialization ---
app = FastAPI(
    title="DNS and Domain Tools API",
    description="An API for various DNS, WHOIS/RDAP, and domain lookup tools.",
    version="1.1.0"
)

# --- Global Exception Handler ---
# Catches any unhandled exceptions and returns a standardized 500 error.
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    error_id = datetime.now().strftime("%Y%m%d%H%M%S")
    logger.error(
        f"Error ID: {error_id} - Unhandled exception for request {request.method} {request.url}: {str(exc)}\n{traceback.format_exc()}"
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal Server Error",
            "error_id": error_id,
            "message": "An unexpected error occurred. Please contact support with the Error ID.",
        },
    )

# --- CORS Middleware ---
# In production, restrict origins to your frontend's domain for security.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins for development
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods (GET, POST, etc.)
    allow_headers=["*"],  # Allows all headers
)

# --- API Routers ---
# Include all the different tool routers with an /api prefix.
app.include_router(whois_rdap_router, prefix="/api", tags=["WHOIS/RDAP"])
app.include_router(vt_bulk_check_router, prefix="/api", tags=["VirusTotal Bulk Check"])
# Also mount VT router without /api so nginx proxy_pass with trailing slash (which strips /api) still reaches it.
app.include_router(vt_bulk_check_router, tags=["VirusTotal Bulk Check"])
# app.include_router(bulk_lookup_router, prefix="/api", tags=["Bulk Lookup"]) # Temporarily disabled

# --- Root Endpoint ---
@app.get("/", summary="API Root", description="Provides basic API information and health status.", tags=["General"])
async def read_root():
    return {"message": "Welcome to the DNS and Domain Tools API. Visit /docs for API documentation."}

##############################################
# DNS Lookup Tool
##############################################

def single_dns_query(domain: str, nameserver: str, record_type: str) -> dict:
    logger.info(f"DNS query request: domain={domain}, nameserver={nameserver}, record_type={record_type}")
    
    # If nameserver is empty or set to 'system_default', use system resolver
    if not nameserver.strip() or nameserver == "system_default":
        try:
            resolver = dns.resolver.Resolver()
            system_nameservers = resolver.nameservers
            if system_nameservers:
                nameserver = system_nameservers[0]  # Use the first system nameserver
                logger.info(f"Using system nameserver: {nameserver}")
            else:
                # Fallback to Google DNS if no system nameservers found
                nameserver = "8.8.8.8"
                logger.warning("No system nameservers found, falling back to Google DNS (8.8.8.8)")
        except Exception as e:
            # Fallback to Google DNS if there's any issue
            nameserver = "8.8.8.8"
            logger.error(f"Error getting system nameservers: {str(e)}. Falling back to Google DNS (8.8.8.8)")
            logger.debug(traceback.format_exc())
    
    result_obj = {
        "name_server": nameserver,
        "text": "",
        "is_authoritative": False
    }

    try:
        addr_info_list = socket.getaddrinfo(nameserver, 53, 0, 0, socket.IPPROTO_UDP)
        logger.debug(f"Resolved nameserver {nameserver} to {[info[4][0] for info in addr_info_list]}")
    except socket.gaierror as e:
        error_msg = f"Error: Could not resolve nameserver {nameserver} ({str(e)})"
        logger.error(error_msg)
        result_obj["text"] = error_msg
        return result_obj

    addresses = []
    for addr_info in addr_info_list:
        family = addr_info[0]
        ip_address = addr_info[4][0]
        addresses.append((family, ip_address))

    for address_family, ns_ip in addresses:
        try:
            rdtype = getattr(dns.rdatatype, record_type)
        except AttributeError as e:
            error_msg = f"Error: Unknown record type '{record_type}'"
            logger.error(f"{error_msg}: {str(e)}")
            result_obj["text"] = error_msg
            return result_obj

        query = dns.message.make_query(domain, rdtype)
        query.flags |= dns.flags.RD

        if address_family == socket.AF_INET6:
            sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        else:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)

        start_time = datetime.now()
        try:
            logger.debug(f"Sending DNS query to {ns_ip} for {domain} ({record_type})")
            response = dns.query.udp(query, ns_ip, timeout=5, sock=sock)
            logger.debug(f"Received response from {ns_ip}")
        except dns.exception.DNSException as e:
            logger.warning(f"DNS exception when querying {ns_ip}: {str(e)}")
            sock.close()
            continue
        except Exception as e:
            logger.error(f"Unexpected error when querying {ns_ip}: {str(e)}")
            logger.debug(traceback.format_exc())
            sock.close()
            continue
        finally:
            sock.close()

        end_time = datetime.now()
        query_time = int((end_time - start_time).total_seconds() * 1000)

        is_auth = bool(response.flags & dns.flags.AA)
        result_obj["is_authoritative"] = is_auth

        flags_text = dns.flags.to_text(response.flags).lower().split()
        rcode_text = dns.rcode.to_text(response.rcode()).upper()

        output_lines = []
        output_lines.append(f"; <<>> DiG 9 <<>> @{nameserver} {domain} {record_type}")
        output_lines.append(";; global options: +cmd")
        output_lines.append(f";; ->>HEADER<<- opcode: QUERY, status: {rcode_text}, id: {response.id}")
        output_lines.append(
            f";; flags: {' '.join(flags_text)}; QUERY: {len(response.question)}, ANSWER: {len(response.answer)}, "
            f"AUTHORITY: {len(response.authority)}, ADDITIONAL: {len(response.additional)}"
        )

        if 'rd' in flags_text and 'ra' not in flags_text:
            output_lines.append(";; WARNING: recursion requested but not available")

        output_lines.append("\n;; QUESTION SECTION:")
        for question in response.question:
            output_lines.append(f";{question.to_text()}")

        if response.answer:
            output_lines.append("\n;; ANSWER SECTION:")
            for answer in response.answer:
                output_lines.append(answer.to_text())
        else:
            output_lines.append("\n;; No answer section.")

        if response.authority:
            output_lines.append("\n;; AUTHORITY SECTION:")
            for authority in response.authority:
                output_lines.append(authority.to_text())
        else:
            output_lines.append("\n;; No authority section.")

        if response.additional:
            output_lines.append("\n;; ADDITIONAL SECTION:")
            for additional in response.additional:
                output_lines.append(additional.to_text())

        output_lines.append(f"\n;; Query time: {query_time} msec")
        output_lines.append(f";; SERVER: {ns_ip}#53({nameserver})")
        output_lines.append(f";; WHEN: {datetime.now().strftime('%a %b %d %H:%M:%S %Y')}")
        output_lines.append(f";; MSG SIZE  rcvd: {len(response.to_wire())}")

        result_obj["text"] = "\n".join(output_lines)
        return result_obj

    error_msg = f"Error: No valid response from any IP of nameserver {nameserver}"
    logger.error(error_msg)
    result_obj["text"] = error_msg
    return result_obj

@app.post("/api/dns-query")
async def dns_query(payload: dict):
    try:
        logger.info(f"Received DNS query request: {payload}")
        
        # Validate required fields
        domain = payload.get("domain")
        record_type = payload.get("record_type")
        nameservers = payload.get("nameservers")

        if not domain:
            error_msg = "Missing required field: 'domain'"
            logger.warning(f"Bad request: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)
            
        if not record_type:
            error_msg = "Missing required field: 'record_type'"
            logger.warning(f"Bad request: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)
        
        # Validate record type
        try:
            getattr(dns.rdatatype, record_type)
        except AttributeError:
            error_msg = f"Invalid record type: '{record_type}'"
            logger.warning(f"Bad request: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)
        
        # For certain record types, nameservers can be optional
        # Use system default resolver if no nameservers are provided
        optional_nameserver_records = ["NS", "SOA", "A", "AAAA", "MX", "TXT", "CNAME", "PTR"]
        if not nameservers and record_type in optional_nameserver_records:
            logger.info("No nameservers provided for NS query, using system default")
            # Use system default nameserver (usually from /etc/resolv.conf)
            try:
                resolver = dns.resolver.Resolver()
                system_nameservers = resolver.nameservers
                if system_nameservers:
                    nameservers = [system_nameservers[0]]  # Use the first system nameserver
                    logger.info(f"Using system nameserver: {system_nameservers[0]}")
                else:
                    # Fallback to Google DNS if no system nameservers found
                    nameservers = ["8.8.8.8"]
                    logger.warning("No system nameservers found, falling back to Google DNS (8.8.8.8)")
            except Exception as e:
                # Fallback to Google DNS if there's any issue
                nameservers = ["8.8.8.8"]
                logger.error(f"Error getting system nameservers: {str(e)}. Falling back to Google DNS (8.8.8.8)")
                logger.debug(traceback.format_exc())
        elif not nameservers:
            error_msg = "Payload must include 'nameservers' (list) for non-NS record types."
            logger.warning(f"Bad request: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)

        if not isinstance(nameservers, list):
            error_msg = "'nameservers' must be a list of hostnames/IP addresses."
            logger.warning(f"Bad request: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)

        # Limit to 4 nameservers for performance reasons
        if len(nameservers) > 4:
            logger.info(f"Request had {len(nameservers)} nameservers, limiting to first 4")
            nameservers = nameservers[:4]

        results = []
        for ns in nameservers:
            single_result = single_dns_query(domain, ns, record_type)
            results.append(single_result)

        logger.info(f"DNS query completed successfully for {domain} ({record_type})")
        return {"results": results}
    
    except HTTPException:
        # Re-raise HTTP exceptions for proper handling
        raise
    except Exception as e:
        # Log unexpected errors
        error_id = datetime.now().strftime("%Y%m%d%H%M%S")
        logger.error(f"Error ID: {error_id} - Unexpected error in dns_query: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error (ID: {error_id}). Please contact support with this ID if the issue persists."
        )

##############################################
# URL Sanitize / Unsanitize / Extract Tool
##############################################

@app.post("/api/sanitize-url")
async def sanitize_url(payload: dict):
    try:
        logger.info(f"Received URL sanitize request")
        urls = payload.get("urls")
        
        if not urls:
            error_msg = "Missing required field: 'urls'"
            logger.warning(f"Bad request: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)
            
        if not isinstance(urls, list):
            error_msg = "'urls' must be provided as a list"
            logger.warning(f"Bad request: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)
        
        logger.debug(f"Sanitizing {len(urls)} URLs")
        results = sanitize_urls(urls)
        logger.info(f"URL sanitization completed successfully for {len(urls)} URLs")
        return {"results": results}
        
    except HTTPException:
        raise
    except Exception as e:
        error_id = datetime.now().strftime("%Y%m%d%H%M%S")
        logger.error(f"Error ID: {error_id} - Unexpected error in sanitize_url: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error (ID: {error_id}). Please contact support with this ID if the issue persists."
        )

@app.post("/api/unsanitize-url")
async def unsanitize_url(payload: dict):
    try:
        logger.info(f"Received URL unsanitize request")
        urls = payload.get("urls")
        
        if not urls:
            error_msg = "Missing required field: 'urls'"
            logger.warning(f"Bad request: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)
            
        if not isinstance(urls, list):
            error_msg = "'urls' must be provided as a list"
            logger.warning(f"Bad request: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)
        
        logger.debug(f"Unsanitizing {len(urls)} URLs")
        results = unsanitize_urls(urls)
        logger.info(f"URL unsanitization completed successfully for {len(urls)} URLs")
        return {"results": results}
        
    except HTTPException:
        raise
    except Exception as e:
        error_id = datetime.now().strftime("%Y%m%d%H%M%S")
        logger.error(f"Error ID: {error_id} - Unexpected error in unsanitize_url: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error (ID: {error_id}). Please contact support with this ID if the issue persists."
        )

@app.post("/api/extract-domains")
async def extract_domains_api(payload: dict):
    try:
        logger.info(f"Received domain extraction request")
        urls = payload.get("urls")
        
        if not urls:
            error_msg = "Missing required field: 'urls'"
            logger.warning(f"Bad request: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)
            
        if not isinstance(urls, list):
            error_msg = "'urls' must be provided as a list"
            logger.warning(f"Bad request: {error_msg}")
            raise HTTPException(status_code=400, detail=error_msg)
        
        logger.debug(f"Extracting domains from {len(urls)} URLs")
        results = extract_domains(urls)
        logger.info(f"Domain extraction completed successfully for {len(urls)} URLs")
        return {"results": results}
        
    except HTTPException:
        raise
    except Exception as e:
        error_id = datetime.now().strftime("%Y%m%d%H%M%S")
        logger.error(f"Error ID: {error_id} - Unexpected error in extract_domains_api: {str(e)}")
        logger.error(traceback.format_exc())
        raise HTTPException(
            status_code=500,
            detail=f"Internal server error (ID: {error_id}). Please contact support with this ID if the issue persists."
        )

##############################################
# Start the app (local testing only)
##############################################

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
