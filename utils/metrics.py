"""
Metrics Calculation Utility for N-gram Automation
Calculates CTR, CVR, CPC, ACOS and other KPIs for N-grams.
"""

import pandas as pd
from typing import Dict, Optional


def calculate_ctr(clicks: float, impressions: float) -> Optional[float]:
    """
    Calculate Click-Through Rate (CTR).
    
    CTR = (Clicks / Impressions) * 100
    
    Args:
        clicks: Number of clicks
        impressions: Number of impressions
        
    Returns:
        CTR as a percentage, or None if impressions is 0
    """
    if impressions <= 0:
        return None
    return round((clicks / impressions) * 100, 2)


def calculate_cvr(orders: float, clicks: float) -> Optional[float]:
    """
    Calculate Conversion Rate (CVR).
    
    CVR = (Orders / Clicks) * 100
    
    Args:
        orders: Number of orders
        clicks: Number of clicks
        
    Returns:
        CVR as a percentage, or None if clicks is 0
    """
    if clicks <= 0:
        return None
    return round((orders / clicks) * 100, 2)


def calculate_cpc(spend: float, clicks: float) -> Optional[float]:
    """
    Calculate Cost Per Click (CPC).
    
    CPC = Spend / Clicks
    
    Args:
        spend: Total spend
        clicks: Number of clicks
        
    Returns:
        CPC, or None if clicks is 0
    """
    if clicks <= 0:
        return None
    return round(spend / clicks, 2)


def calculate_acos(spend: float, sales: float) -> Optional[float]:
    """
    Calculate Advertising Cost of Sales (ACOS).
    
    ACOS = (Spend / Sales) * 100
    
    Args:
        spend: Total spend
        sales: Total sales
        
    Returns:
        ACOS as a percentage, or None if sales is 0
    """
    if sales <= 0:
        return None
    return round((spend / sales) * 100, 2)


def calculate_roas(sales: float, spend: float) -> Optional[float]:
    """
    Calculate Return on Ad Spend (ROAS).
    
    ROAS = Sales / Spend
    
    Args:
        sales: Total sales
        spend: Total spend
        
    Returns:
        ROAS, or None if spend is 0
    """
    if spend <= 0:
        return None
    return round(sales / spend, 2)


def calculate_metrics(row: pd.Series) -> dict:
    """
    Calculate all metrics for a single row of data.
    
    Args:
        row: Series containing impressions, clicks, spend, orders, sales
        
    Returns:
        Dictionary with calculated metrics
    """
    impressions = float(row.get('impressions', 0) or 0)
    clicks = float(row.get('clicks', 0) or 0)
    spend = float(row.get('spend', 0) or 0)
    orders = float(row.get('orders', 0) or 0)
    sales = float(row.get('sales', 0) or 0)
    
    return {
        'ctr': calculate_ctr(clicks, impressions),
        'cvr': calculate_cvr(orders, clicks),
        'cpc': calculate_cpc(spend, clicks),
        'acos': calculate_acos(spend, sales),
        'roas': calculate_roas(sales, spend)
    }


def aggregate_ngram_metrics(df: pd.DataFrame) -> pd.DataFrame:
    """
    Add calculated metrics columns to an N-gram DataFrame.
    
    Args:
        df: DataFrame with ngram, impressions, clicks, spend, orders, sales columns
        
    Returns:
        DataFrame with added CTR, CVR, CPC, ACOS columns
    """
    if df.empty:
        return df
    
    df = df.copy()
    
    # Calculate CTR
    df['ctr'] = df.apply(
        lambda row: calculate_ctr(row['clicks'], row['impressions']),
        axis=1
    )
    
    # Calculate CVR
    df['cvr'] = df.apply(
        lambda row: calculate_cvr(row['orders'], row['clicks']),
        axis=1
    )
    
    # Calculate CPC
    df['cpc'] = df.apply(
        lambda row: calculate_cpc(row['spend'], row['clicks']),
        axis=1
    )
    
    # Calculate ACOS
    df['acos'] = df.apply(
        lambda row: calculate_acos(row['spend'], row['sales']),
        axis=1
    )
    
    return df


def format_metrics_for_display(df: pd.DataFrame) -> pd.DataFrame:
    """
    Format metrics columns for display (add % signs, format numbers).
    
    Args:
        df: DataFrame with metrics columns
        
    Returns:
        DataFrame with formatted display columns
    """
    if df.empty:
        return df
    
    df = df.copy()
    
    # Format percentage columns
    pct_columns = ['ctr', 'cvr', 'acos']
    for col in pct_columns:
        if col in df.columns:
            df[f'{col}_display'] = df[col].apply(
                lambda x: f"{x:.2f}%" if pd.notna(x) else ""
            )
    
    # Format currency columns
    currency_columns = ['spend', 'sales', 'cpc']
    for col in currency_columns:
        if col in df.columns:
            df[f'{col}_display'] = df[col].apply(
                lambda x: f"${x:.2f}" if pd.notna(x) and x > 0 else "$0.00"
            )
    
    return df


def get_campaign_summary(df: pd.DataFrame) -> dict:
    """
    Calculate summary metrics for a campaign.
    
    Args:
        df: DataFrame with campaign data
        
    Returns:
        Dictionary with aggregated metrics
    """
    total_impressions = df['impressions'].sum() if 'impressions' in df.columns else 0
    total_clicks = df['clicks'].sum() if 'clicks' in df.columns else 0
    total_spend = df['spend'].sum() if 'spend' in df.columns else 0
    total_orders = df['orders'].sum() if 'orders' in df.columns else 0
    total_sales = df['sales'].sum() if 'sales' in df.columns else 0
    
    return {
        'total_impressions': int(total_impressions),
        'total_clicks': int(total_clicks),
        'total_spend': round(total_spend, 2),
        'total_orders': int(total_orders),
        'total_sales': round(total_sales, 2),
        'overall_ctr': calculate_ctr(total_clicks, total_impressions),
        'overall_cvr': calculate_cvr(total_orders, total_clicks),
        'overall_cpc': calculate_cpc(total_spend, total_clicks),
        'overall_acos': calculate_acos(total_spend, total_sales)
    }
