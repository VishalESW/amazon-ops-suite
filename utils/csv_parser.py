"""
CSV Parser Utility for N-gram Automation
Handles parsing raw search term data, filtering ASINs, and grouping by campaign.
"""

import pandas as pd
import re
from typing import Dict, List, Tuple


# Column mapping for Amazon Search Term Report
COLUMN_MAPPING = {
    'date': ['Start Date', 'End Date', 'Date', 'date'],
    'portfolio': ['Portfolio name', 'Portfolio Name', 'portfolio name', 'Portfolio'],
    'currency': ['Currency', 'currency'],
    'campaign': ['Campaign Name', 'Campaign name', 'campaign name', 'Campaign', 'CampaignName', 'campaign_name'],
    'campaign_status': ['Campaign Status', 'campaign status', 'Status', 'state', 'Campaign State'],
    'ad_group': ['Ad Group Name', 'Ad Group name', 'ad group name', 'Ad Group', 'AdGroup', 'ad_group'],
    'targeting': ['Targeting', 'targeting', 'Targeting Expression'],
    'match_type': ['Match Type', 'Match type', 'match type'],
    'search_term': ['Customer Search Term', 'Search Term', 'search term', 'Search term', 'Keyword', 'keyword', 'Query', 'query', 'SearchTerm', 'search_term'],
    'impressions': ['Impressions', 'impressions', 'Impr.', 'impr'],
    'clicks': ['Clicks', 'clicks'],
    'ctr': ['Click-Thru Rate (CTR)', 'Click-through rate (CTR)', 'CTR', 'ctr', 'Click-Through Rate'],
    'cpc': ['Cost Per Click (CPC)', 'Cost per click (CPC)', 'CPC', 'cpc'],
    'spend': ['Spend', 'spend', 'Cost', 'cost', 'Total Spend'],
    'sales': ['7 Day Total Sales ($)', '7 Day Total Sales', '7 Day Total Sales ', 'Sales', 'sales', 'Total Sales'],
    'acos': ['Total Advertising Cost of Sales (ACOS)', 'Total Advertising Cost of Sales (ACoS)', 'ACOS', 'acos', 'Acos'],
    'roas': ['Total Return on Advertising Spend (ROAS)', 'ROAS', 'roas'],
    'orders': ['7 Day Total Orders (#)', 'Orders', 'orders', 'Total Orders'],
    'units': ['7 Day Total Units (#)', 'Units', 'units', 'Total Units'],
    'conversion_rate': ['7 Day Conversion Rate', 'Conversion Rate', 'conversion rate']
}


def find_column(df: pd.DataFrame, possible_names: List[str]) -> str:
    """Find the actual column name from a list of possible names (case-insensitive)."""
    # First try exact match - prioritize exact matches
    for name in possible_names:
        if name in df.columns:
            return name
    
    # Try case-insensitive match
    df_columns_lower = {col.lower(): col for col in df.columns}
    for name in possible_names:
        if name.lower() in df_columns_lower:
            return df_columns_lower[name.lower()]
    
    return None


def find_best_column(df: pd.DataFrame, possible_names: List[str]) -> str:
    """Find the best matching column."""
    # First pass: exact match for any of the possible names
    for name in possible_names:
        if name in df.columns:
            return name
    
    # Second pass: case-insensitive match
    df_columns_lower = {col.lower(): col for col in df.columns}
    for name in possible_names:
        if name.lower() in df_columns_lower:
            return df_columns_lower[name.lower()]
    
    return None


def find_column_fuzzy(df: pd.DataFrame, keywords: List[str]) -> str:
    """Find column by checking if any keyword is in the column name."""
    for col in df.columns:
        col_lower = col.lower()
        for keyword in keywords:
            if keyword.lower() in col_lower:
                return col
    return None


def standardize_columns(df: pd.DataFrame) -> pd.DataFrame:
    """Standardize column names to our internal naming convention."""
    column_renames = {}
    
    # Key columns where we want to prioritize 'converted' version
    priority_columns = ['spend', 'sales', 'cpc']
    
    # First pass: rename priority columns with 'converted' priority
    for standard_name in priority_columns:
        if standard_name in COLUMN_MAPPING:
            possible_names = COLUMN_MAPPING[standard_name]
            actual_name = find_best_column(df, possible_names)
            if actual_name and actual_name not in column_renames:
                column_renames[actual_name] = standard_name
    
    # Second pass: rename remaining columns
    for standard_name, possible_names in COLUMN_MAPPING.items():
        if standard_name in priority_columns:
            continue  # Skip already processed
            
        actual_name = find_column(df, possible_names)
        if actual_name and actual_name not in column_renames:
            column_renames[actual_name] = standard_name
    
    df = df.rename(columns=column_renames)
    return df


def detect_file_type(file_path: str) -> str:
    """Detect if file is CSV or Excel based on extension."""
    import os
    ext = os.path.splitext(file_path)[1].lower()
    
    # Always trust the file extension
    if ext in ['.xlsx', '.xls', '.xlsm']:
        return 'excel'
    elif ext in ['.csv', '.txt']:
        return 'csv'
    else:
        return 'csv'


def parse_csv(file_path: str) -> pd.DataFrame:
    """
    Parse a CSV or Excel file and return a standardized DataFrame.
    Handles various file formats and encoding issues.
    
    Args:
        file_path: Path to the file
        
    Returns:
        Standardized DataFrame with consistent column names
    """
    import sys
    
    df = None
    file_type = detect_file_type(file_path)
    print(f"DEBUG: Detected file type: {file_type}", file=sys.stderr)
    
    # Try reading as Excel first if it looks like Excel
    if file_type == 'excel':
        try:
            # Try different Excel engines
            for engine in ['openpyxl', 'xlrd']:
                try:
                    df = pd.read_excel(file_path, engine=engine)
                    print(f"DEBUG: Successfully read as Excel file with {engine}", file=sys.stderr)
                    break
                except Exception as e1:
                    try:
                        df = pd.read_excel(file_path, engine=engine, data_only=True)
                        print(f"DEBUG: Successfully read as Excel with data_only", file=sys.stderr)
                        break
                    except Exception as e2:
                        continue
        except Exception as e:
            print(f"DEBUG: All Excel read attempts failed: {e}", file=sys.stderr)
    
    # If Excel failed or it's CSV, try CSV parsing
    if df is None:
        encodings = ['utf-8-sig', 'utf-8', 'latin-1', 'cp1252', 'iso-8859-1', 'windows-1252']
        
        for encoding in encodings:
            try:
                df = pd.read_csv(file_path, encoding=encoding, on_bad_lines='skip', low_memory=False)
                print(f"DEBUG: Successfully read CSV with {encoding} encoding", file=sys.stderr)
                break
            except Exception:
                try:
                    df = pd.read_csv(file_path, encoding=encoding, engine='python', on_bad_lines='skip')
                    print(f"DEBUG: Successfully read CSV with {encoding} encoding (python engine)", file=sys.stderr)
                    break
                except Exception:
                    try:
                        df = pd.read_csv(file_path, encoding=encoding, sep=';', on_bad_lines='skip')
                        print(f"DEBUG: Successfully read CSV with {encoding} encoding (semicolon)", file=sys.stderr)
                        break
                    except Exception:
                        continue
    
    # Last resort: manual parsing
    if df is None or len(df.columns) == 0 or all(len(str(col)) > 50 for col in df.columns):
        try:
            with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
            
            # Find header line
            lines = content.split('\n')
            header_idx = 0
            for i, line in enumerate(lines[:30]):
                if any(keyword in line for keyword in ['Campaign', 'Search Term', 'Customer', 'Impressions', 'Clicks']):
                    header_idx = i
                    break
            
            # Parse from header
            from io import StringIO
            clean_content = '\n'.join(lines[header_idx:])
            df = pd.read_csv(StringIO(clean_content), on_bad_lines='skip', engine='python')
            print(f"DEBUG: Successfully read with manual parsing", file=sys.stderr)
        except Exception as e:
            raise Exception(f"Unable to parse file. Please ensure it's a valid CSV or Excel file. Error: {str(e)}")
    
    # Validate we got meaningful data
    if df is None or len(df.columns) == 0:
        raise Exception("Could not read any data from the file. Please check the file format.")
    
    # Check if columns look garbled (very long column names suggest binary/encrypted data)
    if all(len(str(col)) > 30 for col in df.columns):
        raise Exception("File appears to be corrupted, encrypted, or in an unsupported format. Please upload a valid CSV or Excel file.")
    
    print(f"DEBUG: Original columns: {list(df.columns)}", file=sys.stderr)
    
    # Standardize column names
    df = standardize_columns(df)
    
    print(f"DEBUG: Standardized columns: {list(df.columns)}", file=sys.stderr)
    
    # Debug: Show which spend/sales columns were detected
    print(f"DEBUG: Spend column selected: {df.columns.get_loc('spend') if 'spend' in df.columns else 'NOT FOUND'}", file=sys.stderr)
    print(f"DEBUG: Sales column selected: {df.columns.get_loc('sales') if 'sales' in df.columns else 'NOT FOUND'}", file=sys.stderr)
    
    # Debug: Show sample spend values BEFORE conversion
    if 'spend' in df.columns:
        print(f"DEBUG: Spend sample BEFORE conversion: {df['spend'].head(3).tolist()}", file=sys.stderr)
    
    # Clean up numeric columns - handle ALL currency symbols including CAD ($), MXN ($), EUR (€), GBP (£), JPY (¥), etc.
    numeric_columns = ['impressions', 'clicks', 'spend', 'sales', 'orders', 'units']
    for col in numeric_columns:
        if col in df.columns:
            # First remove currency codes like CA$, MXN$, US$, etc. (2-3 letter codes with $)
            df[col] = df[col].astype(str).str.replace(r'[A-Z]{1,3}\$', '', regex=True)
            # Then remove remaining currency symbols
            df[col] = df[col].astype(str).str.replace(r'[\$,€£¥₹₽₩₱]', '', regex=True)
            # Remove thousands separators and whitespace
            df[col] = df[col].astype(str).str.replace(r'[,\s]', '', regex=True)
            # Handle parentheses for negative numbers (accounting format)
            df[col] = df[col].str.replace(r'^\((.*)\)$', r'-\1', regex=True)
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    
    pct_columns = ['ctr', 'acos', 'conversion_rate']
    for col in pct_columns:
        if col in df.columns:
            df[col] = df[col].astype(str).str.replace(r'[\$,€£¥₹%‰]', '', regex=True)
            df[col] = df[col].astype(str).str.replace(r'[,\s]', '', regex=True)
            df[col] = pd.to_numeric(df[col], errors='coerce').fillna(0)
    
    # Debug: Show sample spend values AFTER conversion
    if 'spend' in df.columns:
        print(f"DEBUG: Spend sample AFTER conversion: {df['spend'].head(3).tolist()}", file=sys.stderr)
        print(f"DEBUG: Total spend: {df['spend'].sum()}", file=sys.stderr)
    
    return df


def filter_asins(df: pd.DataFrame) -> Tuple[pd.DataFrame, pd.DataFrame, int]:
    """
    Filter out rows where search term starts with 'B0' (ASIN pattern).
    
    Args:
        df: DataFrame with search term data
        
    Returns:
        Tuple of (filtered DataFrame without ASINs, DataFrame with only ASINs, count of ASINs)
    """
    if 'search_term' not in df.columns:
        return df, pd.DataFrame(), 0
    
    # Count rows before filtering
    original_count = len(df)
    
    # Filter out search terms starting with B0 (case insensitive)
    # ASIN pattern: starts with B0 followed by alphanumeric characters
    asin_pattern = r'^[Bb]0[A-Za-z0-9]+'
    
    # Mask for non-ASIN rows
    mask = ~df['search_term'].astype(str).str.match(asin_pattern, na=False)
    df_filtered = df[mask].copy()
    
    # Get ASIN rows
    df_asins = df[~mask].copy()
    
    asin_count = len(df_asins)
    
    return df_filtered, df_asins, asin_count


def group_by_campaign(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """
    Group the data by campaign name.
    
    Args:
        df: DataFrame with search term data
        
    Returns:
        Dictionary mapping campaign names to their DataFrames
    """
    if 'campaign' not in df.columns:
        # If no campaign column, return all data under a single key
        return {'All Campaigns': df}
    
    campaigns = {}
    
    for campaign_name, group_df in df.groupby('campaign', dropna=False):
        # Handle NaN campaign names
        if pd.isna(campaign_name):
            campaign_name = 'Unknown Campaign'
        
        campaigns[campaign_name] = group_df.reset_index(drop=True)
    
    return campaigns


def get_data_summary(df: pd.DataFrame) -> dict:
    """
    Get a summary of the data.
    
    Args:
        df: DataFrame with search term data
        
    Returns:
        Dictionary with summary statistics
    """
    summary = {
        'total_rows': len(df),
        'total_campaigns': df['campaign'].nunique() if 'campaign' in df.columns else 0,
        'total_search_terms': df['search_term'].nunique() if 'search_term' in df.columns else 0,
        'total_impressions': df['impressions'].sum() if 'impressions' in df.columns else 0,
        'total_clicks': df['clicks'].sum() if 'clicks' in df.columns else 0,
        'total_spend': df['spend'].sum() if 'spend' in df.columns else 0,
        'total_sales': df['sales'].sum() if 'sales' in df.columns else 0,
        'total_orders': df['orders'].sum() if 'orders' in df.columns else 0,
    }
    
    return summary


def filter_active_campaigns(df: pd.DataFrame) -> pd.DataFrame:
    """
    Filter to include only active campaigns.
    Amazon reports can have campaign status column.
    
    Args:
        df: DataFrame with campaign data
        
    Returns:
        DataFrame with only active campaigns
    """
    if 'campaign_status' not in df.columns:
        # No status column - include all
        return df
    
    # Check unique status values for debugging
    unique_status = df['campaign_status'].unique()
    print(f"DEBUG: Campaign statuses found: {unique_status}", file=__import__('sys').stderr)
    
    # Define active status values (case-insensitive)
    active_values = ['active', 'enabled', 'running', 'on', 'live', 'yes', 'true', '1']
    
    # Create mask for active campaigns
    df = df.copy()
    df['campaign_status'] = df['campaign_status'].astype(str).str.lower().str.strip()
    
    mask = df['campaign_status'].isin(active_values)
    
    # Also include rows with no status (some reports don't have status column properly filled)
    status_null = df['campaign_status'].isin(['', 'nan', 'none', 'null', 'na'])
    
    filtered_df = df[mask | status_null].copy()
    
    print(f"DEBUG: Rows before filtering: {len(df)}, after: {len(filtered_df)}", file=__import__('sys').stderr)
    
    return filtered_df


def validate_csv(df: pd.DataFrame) -> Tuple[bool, List[str]]:
    """
    Validate that the CSV has the required columns.
    
    Args:
        df: DataFrame to validate
        
    Returns:
        Tuple of (is_valid, list of missing required columns)
    """
    required_columns = ['search_term', 'impressions', 'clicks', 'spend']
    missing = []
    
    for col in required_columns:
        if col not in df.columns:
            missing.append(col)
    
    return len(missing) == 0, missing
