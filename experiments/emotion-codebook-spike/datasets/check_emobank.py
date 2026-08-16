"""P0: verify EmoBank row counts per split (provenance check)."""
import pandas as pd

df = pd.read_csv("datasets/raw/emobank.csv")
print("total rows:", len(df))
print("split counts:", df["split"].value_counts().to_dict())
print("cols:", list(df.columns))
print("V range:", float(df["V"].min()), float(df["V"].max()))
