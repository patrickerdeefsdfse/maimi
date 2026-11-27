"""Private HTTP entry point for the employee-scoped sales-order workflow."""
import uvicorn
from .cloud import create_app

def main():
    uvicorn.run(create_app(),host="127.0.0.1",port=8765,access_log=False)

if __name__=="__main__":
    main()
