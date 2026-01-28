#!/usr/bin/env python3
import asyncio
import json
import whois  # Use the correct 'whois' package, not 'pywhois'
import httpx
import subprocess
import logging
import os
from typing import List, Dict, Any, Optional

# Re-export the functionality from whois_rdap_service for compatibility
from whois_rdap_service import query_whois, query_rdap
