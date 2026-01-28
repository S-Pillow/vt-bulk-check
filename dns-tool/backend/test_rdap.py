#!/usr/bin/env python3
import asyncio
import httpx
import json
import logging

logging.basicConfig(level=logging.DEBUG,
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("rdap-test")

async def test_rdap_lookup(domain):
    logger.info(f"Testing RDAP lookup for domain: {domain}")
    
    # First find the correct RDAP server for the TLD
    tld = domain.split('.')[-1]
    async with httpx.AsyncClient() as client:
        try:
            # Get RDAP server from IANA bootstrap registry
            bootstrap_url = f"https://data.iana.org/rdap/{tld}.json"
            logger.info(f"Fetching RDAP server from: {bootstrap_url}")
            bootstrap_response = await client.get(bootstrap_url, timeout=10.0)
            bootstrap_response.raise_for_status()
            bootstrap_data = bootstrap_response.json()
            
            logger.info(f"Bootstrap data: {bootstrap_data}")
            
            # Extract RDAP server URL
            rdap_server_url = None
            for service_entry in bootstrap_data.get('services', []):
                if tld in service_entry[0]:
                    if len(service_entry[1]) > 0:
                        rdap_server_url = service_entry[1][0]
                        break
            
            if not rdap_server_url:
                rdap_query_url = f"https://rdap.org/domain/{domain}" # Fallback
                logger.info(f"No RDAP server found, using fallback: {rdap_query_url}")
            elif not rdap_server_url.endswith('/'):
                rdap_query_url = f"{rdap_server_url}domain/{domain}"
                logger.info(f"RDAP server URL: {rdap_query_url}")
            else:
                rdap_query_url = f"{rdap_server_url}domain/{domain}"
                logger.info(f"RDAP server URL: {rdap_query_url}")
                
            # Query the RDAP server
            logger.info(f"Querying RDAP server: {rdap_query_url}")
            response = await client.get(rdap_query_url, timeout=10.0, follow_redirects=True)
            response.raise_for_status()
            data = response.json()
            
            # Log the entire response for debugging
            logger.info(f"RDAP response data keys: {list(data.keys())}")
            logger.info(f"Full RDAP response: {json.dumps(data, indent=2)}")
            
            # Specifically check for status fields
            if 'status' in data:
                logger.info(f"Status found in data['status']: {data['status']}")
            if 'domainStatus' in data:
                logger.info(f"Status found in data['domainStatus']: {data['domainStatus']}")
                
            # Look for any key that might contain status information
            for key in data.keys():
                if 'status' in key.lower():
                    logger.info(f"Possible status in key {key}: {data[key]}")
                    
        except httpx.HTTPError as e:
            logger.error(f"HTTP error: {str(e)}")
        except Exception as e:
            logger.error(f"Error during RDAP lookup: {str(e)}")

async def main():
    await test_rdap_lookup("neustar.biz")

if __name__ == "__main__":
    asyncio.run(main())
