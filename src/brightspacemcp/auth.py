import os
from dotenv import load_dotenv
load_dotenv()

def return_cookies():
    d2lSessionVal = str(os.getenv("d2lSessionVal"))
    d2lSecureSessionVal = str(os.getenv("d2lSecureSessionVal"))
    return {'d2lSessionVal':d2lSessionVal, 'd2lSecureSessionVal':d2lSecureSessionVal}
