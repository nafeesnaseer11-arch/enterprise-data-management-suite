"""Generate the sample AWS estate:  python -m scripts.generate_sample_data"""
from edms.config import ROOT
from edms.sample_data import generate

if __name__ == "__main__":
    generate(ROOT / "data" / "aws_s3", ROOT / "data" / "aws_rds")
    print("Sample AWS estate generated under data/aws_s3 and data/aws_rds")
