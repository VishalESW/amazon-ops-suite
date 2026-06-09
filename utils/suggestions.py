"""
NE/NP Suggestions Utility for N-gram Automation
Auto-flags N-grams as Negative Exact (NE) or Negative Phrase (NP) candidates.
"""

import pandas as pd
from typing import Dict, Optional, Tuple


# Default thresholds for flagging
DEFAULT_THRESHOLDS = {
    'min_clicks_for_ne': 3,      # Minimum clicks before flagging as NE
    'min_spend_for_np': 0.01,    # Minimum spend to consider for NP
    'high_acos_threshold': 50,   # ACOS above this is flagged
    'low_ctr_threshold': 0.3,    # CTR below this is flagged
}


def should_flag_as_ne(row: pd.Series, thresholds: dict = None) -> bool:
    """
    Determine if an N-gram should be flagged as Negative Exact (NE).
    
    Criteria: High clicks but zero orders
    
    Args:
        row: Series with clicks, orders data
        thresholds: Custom threshold values
        
    Returns:
        True if should be flagged as NE
    """
    thresholds = thresholds or DEFAULT_THRESHOLDS
    
    clicks = float(row.get('clicks', 0) or 0)
    orders = float(row.get('orders', 0) or 0)
    
    # Flag if clicks >= threshold AND orders = 0
    if clicks >= thresholds['min_clicks_for_ne'] and orders == 0:
        return True
    
    return False


def should_flag_as_np(row: pd.Series, thresholds: dict = None) -> bool:
    """
    Determine if an N-gram should be flagged as Negative Phrase (NP).
    
    Criteria: Has spend but zero sales
    
    Args:
        row: Series with spend, sales data
        thresholds: Custom threshold values
        
    Returns:
        True if should be flagged as NP
    """
    thresholds = thresholds or DEFAULT_THRESHOLDS
    
    spend = float(row.get('spend', 0) or 0)
    sales = float(row.get('sales', 0) or 0)
    
    # Flag if spend > threshold AND sales = 0
    if spend >= thresholds['min_spend_for_np'] and sales == 0:
        return True
    
    return False


def get_suggestion(row: pd.Series, thresholds: dict = None) -> str:
    """
    Get the suggestion (NE, NP, or empty) for an N-gram.
    
    Args:
        row: Series with metrics data
        thresholds: Custom threshold values
        
    Returns:
        'NE', 'NP', or '' (empty string)
    """
    # Check NE first (more specific)
    if should_flag_as_ne(row, thresholds):
        return 'NE'
    
    # Then check NP
    if should_flag_as_np(row, thresholds):
        return 'NP'
    
    return ''


def suggest_negatives(df: pd.DataFrame, thresholds: dict = None) -> pd.DataFrame:
    """
    Add NE/NP suggestion column to a DataFrame.
    
    Args:
        df: DataFrame with N-gram metrics
        thresholds: Custom threshold values
        
    Returns:
        DataFrame with added 'suggestion' column
    """
    if df.empty:
        df['suggestion'] = ''
        df['comments'] = ''
        return df
    
    df = df.copy()
    
    # Add suggestion column
    df['suggestion'] = df.apply(
        lambda row: get_suggestion(row, thresholds),
        axis=1
    )
    
    # Add comments explaining the suggestion
    df['comments'] = df.apply(
        lambda row: get_suggestion_comment(row, thresholds),
        axis=1
    )
    
    return df


def get_suggestion_comment(row: pd.Series, thresholds: dict = None) -> str:
    """
    Get a comment explaining the suggestion.
    
    Args:
        row: Series with metrics data
        thresholds: Custom threshold values
        
    Returns:
        Comment string explaining the flag
    """
    thresholds = thresholds or DEFAULT_THRESHOLDS
    
    clicks = float(row.get('clicks', 0) or 0)
    orders = float(row.get('orders', 0) or 0)
    spend = float(row.get('spend', 0) or 0)
    sales = float(row.get('sales', 0) or 0)
    
    comments = []
    
    # Check for high clicks, zero orders
    if clicks >= thresholds['min_clicks_for_ne'] and orders == 0:
        comments.append(f"{int(clicks)} clicks, 0 orders")
    
    # Check for spend but no sales
    if spend > 0 and sales == 0:
        comments.append(f"${spend:.2f} spent, $0 sales")
    
    return '; '.join(comments)


def get_suggestion_summary(df: pd.DataFrame) -> Dict[str, int]:
    """
    Get a summary of suggestions in a DataFrame.
    
    Args:
        df: DataFrame with suggestion column
        
    Returns:
        Dictionary with counts of NE, NP, and total flagged
    """
    if 'suggestion' not in df.columns:
        return {'ne_count': 0, 'np_count': 0, 'total_flagged': 0}
    
    ne_count = (df['suggestion'] == 'NE').sum()
    np_count = (df['suggestion'] == 'NP').sum()
    
    return {
        'ne_count': int(ne_count),
        'np_count': int(np_count),
        'total_flagged': int(ne_count + np_count)
    }


def apply_custom_rules(df: pd.DataFrame, rules: list) -> pd.DataFrame:
    """
    Apply custom flagging rules to a DataFrame.
    
    Args:
        df: DataFrame with N-gram metrics
        rules: List of rule dictionaries with 'field', 'operator', 'value', 'flag'
        
    Returns:
        DataFrame with updated suggestions
    
    Example rule:
        {'field': 'acos', 'operator': '>', 'value': 50, 'flag': 'NP'}
    """
    if df.empty or not rules:
        return df
    
    df = df.copy()
    
    for rule in rules:
        field = rule.get('field')
        operator = rule.get('operator')
        value = rule.get('value')
        flag = rule.get('flag', 'NP')
        
        if field not in df.columns:
            continue
        
        # Apply rule based on operator
        if operator == '>':
            mask = (df[field] > value) & (df['suggestion'] == '')
        elif operator == '<':
            mask = (df[field] < value) & (df['suggestion'] == '')
        elif operator == '>=':
            mask = (df[field] >= value) & (df['suggestion'] == '')
        elif operator == '<=':
            mask = (df[field] <= value) & (df['suggestion'] == '')
        elif operator == '==':
            mask = (df[field] == value) & (df['suggestion'] == '')
        else:
            continue
        
        df.loc[mask, 'suggestion'] = flag
    
    return df


def filter_flagged_only(df: pd.DataFrame) -> pd.DataFrame:
    """
    Filter DataFrame to show only flagged N-grams.
    
    Args:
        df: DataFrame with suggestion column
        
    Returns:
        Filtered DataFrame with only NE/NP flagged rows
    """
    if 'suggestion' not in df.columns:
        return df
    
    return df[df['suggestion'].isin(['NE', 'NP'])].copy()
