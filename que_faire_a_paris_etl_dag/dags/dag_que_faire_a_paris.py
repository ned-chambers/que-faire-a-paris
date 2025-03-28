from airflow import DAG
from airflow.operators.python import PythonOperator
from airflow.providers.postgres.hooks.postgres import PostgresHook
import pandas as pd
import requests
import io
import datetime as dt
import re
import logging
from datetime import datetime

# Configure logging
logging.basicConfig(level=logging.INFO)

# Placeholder URLs
CSV_URL = "https://opendata.paris.fr/api/explore/v2.1/catalog/datasets/que-faire-a-paris-/exports/csv?lang=fr&timezone=Europe%2FBerlin&use_labels=true&delimiter=%3B"
POSTGRES_CONN_ID = "postgres_default"  # Airflow Postgres connection

default_args = {
    "owner": "airflow",
    "start_date": datetime(2025, 2, 1),
}

dag = DAG(
    "etl_que_faire_a_paris",
    default_args=default_args,
    schedule_interval="@daily",
    catchup=False,
)

def extract_data():
    """Extracts data from the Paris Open Data API and saves it as a CSV file."""
    try:
        response = requests.get(CSV_URL)
        response.raise_for_status()
        df = pd.read_csv(io.StringIO(response.text), sep=";", encoding="utf-8")

        # Save raw data
        df.to_csv("/tmp/que_faire_a_paris_raw.csv", index=False)
        logging.info("✅ Extraction completed successfully!")
    
    except Exception as e:
        logging.error(f"❌ Extraction failed: {str(e)}")
        raise

def transform_data():
    """Loads the raw CSV, applies transformations, and saves the cleaned data."""
    try:
        # Read the CSV using auto-detection for delimiter and skipping bad lines
        df = pd.read_csv(
            "/tmp/que_faire_a_paris_raw.csv", 
            sep=None, 
            engine="python", 
            encoding="utf-8", 
            on_bad_lines="skip"
        )

        # Standardize column names
        df.columns = df.columns.str.strip()

        # Check if required columns exist
        columns_to_keep = [
            "ID", "URL", "Titre", "Chapeau", "Date de début", "Date de fin",
            "Mots clés", "Nom du lieu", "Adresse du lieu", "Code postal", "Ville",
            "Coordonnées géographiques", "Accès PMR", "Accès mal voyant", "Accès mal entendant",
            "Type de prix", "URL de réservation", "Date de mise à jour", "audience"
        ]

        missing_columns = [col for col in columns_to_keep if col not in df.columns]
        if missing_columns:
            logging.error(f"❌ Missing columns in dataset: {missing_columns}")
            raise ValueError(f"Missing columns: {missing_columns}")

        df = df[columns_to_keep]
        df.drop_duplicates(inplace=True)

        # Handle missing values
        fillna_values = {
            "Titre": "N/A", "Chapeau": "N/A", "Mots clés": "N/A", "Nom du lieu": "N/A", 
            "Adresse du lieu": "N/A", "URL de réservation": "N/A", "Code postal": "75000", 
            "Ville": "Paris"
        }
        df.fillna(fillna_values, inplace=True)

        today = dt.datetime.today()
        last_day_of_year = dt.datetime(today.year, 12, 31)
        df["Date de début"].fillna(today, inplace=True)
        df["Date de fin"].fillna(last_day_of_year, inplace=True)

        # Fix missing "Date de mise à jour"
        if df["Date de mise à jour"].notna().any():
            df["Date de mise à jour"].fillna(df["Date de mise à jour"].max(), inplace=True)
        else:
            df["Date de mise à jour"].fillna(today, inplace=True)

        # Convert the columns to datetime format and extract Date and Time separately
        for col in ["Date de début", "Date de fin", "Date de mise à jour"]:
            # Convert to datetime, handling timezone issues and invalid formats
            df[col] = pd.to_datetime(df[col], errors='coerce', utc=True)
            
            # Identify rows where conversion failed
            if df[col].isna().sum() > 0:
                logging.warning(f"⚠️ Warning: {df[col].isna().sum()} invalid datetime values found in '{col}'")

            # Extract Date and Time
            df[f"{col} (Date)"] = df[col].dt.date
            df[f"{col} (Time)"] = df[col].dt.time

        # Standardize accessibility information
        df['Accès PMR'] = df['Accès PMR'].apply(lambda x: 'Oui' if x == 1 else "Aucune information")
        df['Accès mal voyant'] = df['Accès mal voyant'].apply(lambda x: 'Oui' if x == 1 else "Aucune information")
        df['Accès mal entendant'] = df['Accès mal entendant'].apply(lambda x: 'Oui' if x == 1 else "Aucune information")

        # Create "Accessibilité" column
        def format_accessibility(row):
            access_list = []
            
            if row['Accès PMR'] == "Oui":
                access_list.append("personnes à mobilité réduite")
            if row['Accès mal voyant'] == "Oui":
                access_list.append("mal voyant")
            if row['Accès mal entendant'] == "Oui":
                access_list.append("mal entendant")
            
            # Only add "Accès" if there are accessibility features
            if access_list:
                return "Accès " + " + ".join(access_list)
            else:
                return "Aucune information"

        df['Accessibilité'] = df.apply(format_accessibility, axis=1)

        # Clean postal codes
        def clean_code_postal(code):
            code = str(code).strip()
            code = re.sub(r'\s+', '', code)
            code = re.sub(r'\D+$', '', code)
            if len(code) == 4:
                code = code + "0"
            if not re.match(r'^\d{5}$', code):
                code = "75000"
            return code
        df["Code postal"] = df["Code postal"].apply(clean_code_postal)
        # Convert "Code postal" to string
        df["Code postal"] = df["Code postal"].astype(str)

        # Normalize city names
        def clean_ville(ville):
            ville = str(ville).strip()
            if ville.startswith("Paris"):
                ville = "Paris"
            return ville.title()
        df["Ville"] = df["Ville"].apply(clean_ville)

        # Split the "Coordonnées géographiques" column into Latitude and Longitude
        df[['Latitude', 'Longitude']] = df['Coordonnées géographiques'].str.split(', ', expand=True)

        # Convert to numeric (handles missing or corrupted values)
        df['Latitude'] = pd.to_numeric(df['Latitude'], errors='coerce')
        df['Longitude'] = pd.to_numeric(df['Longitude'], errors='coerce')

        # Drop rows where Latitude or Longitude couldn't be converted
        df = df.dropna(subset=['Latitude', 'Longitude'])

        # Convert pricing type to title case
        df["Type de prix"] = df["Type de prix"].astype(str).str.title()
        # Change values to "Aucune information" if "Nan"
        df["Type de prix"] = df["Type de prix"].replace("Nan", "Aucune information")

        # Save transformed CSV
        df.to_csv("/tmp/que_faire_a_paris_transformed.csv", index=False)
        logging.info("✅ Transformation completed successfully!")
    
    except Exception as e:
        logging.error(f"❌ Transformation failed: {str(e)}")
        raise

def load_data():
    """Loads the transformed data into PostgreSQL."""
    try:
        pg_hook = PostgresHook(postgres_conn_id=POSTGRES_CONN_ID)
        engine = pg_hook.get_sqlalchemy_engine()
        df = pd.read_csv("/tmp/que_faire_a_paris_transformed.csv")
        df.to_sql("que_faire_a_paris", engine, if_exists="replace", index=False)
        logging.info("✅ Loading completed successfully!")
    
    except Exception as e:
        logging.error(f"❌ Loading failed: {str(e)}")
        raise

# Define tasks
extract_task = PythonOperator(
    task_id="extract_data",
    python_callable=extract_data,
    dag=dag,
)

transform_task = PythonOperator(
    task_id="transform_data",
    python_callable=transform_data,
    dag=dag,
)

load_task = PythonOperator(
    task_id="load_data",
    python_callable=load_data,
    dag=dag,
)

# Task dependencies
extract_task >> transform_task >> load_task
