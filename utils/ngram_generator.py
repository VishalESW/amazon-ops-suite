"""
N-gram Generator Utility for N-gram Automation
Extracts monograms, bigrams, and trigrams from search terms.
"""

import pandas as pd
import re
from typing import Dict, List, Set, Tuple
from collections import defaultdict


def clean_search_term(term: str) -> str:
    """
    Clean a search term by removing special characters and extra whitespace.
    
    Args:
        term: Raw search term
        
    Returns:
        Cleaned search term
    """
    if pd.isna(term) or not isinstance(term, str):
        return ""
    
    # Convert to lowercase
    term = term.lower().strip()
    
    # Remove special characters but keep spaces
    term = re.sub(r'[^\w\s]', ' ', term)
    
    # Remove extra whitespace
    term = ' '.join(term.split())
    
    return term


def tokenize(term: str) -> List[str]:
    """
    Tokenize a search term into words.
    
    Args:
        term: Cleaned search term
        
    Returns:
        List of words
    """
    if not term:
        return []
    
    return term.split()


def extract_monograms(words: List[str]) -> List[str]:
    """
    Extract monograms (single words) from a list of words.
    
    Args:
        words: List of words from a search term
        
    Returns:
        List of monograms
    """
    return words.copy()


def extract_bigrams(words: List[str]) -> List[str]:
    """
    Extract bigrams (two-word combinations) from a list of words.
    
    Args:
        words: List of words from a search term
        
    Returns:
        List of bigrams
    """
    if len(words) < 2:
        return []
    
    bigrams = []
    for i in range(len(words) - 1):
        bigram = f"{words[i]} {words[i+1]}"
        bigrams.append(bigram)
    
    return bigrams


def extract_trigrams(words: List[str]) -> List[str]:
    """
    Extract trigrams (three-word combinations) from a list of words.
    
    Args:
        words: List of words from a search term
        
    Returns:
        List of trigrams
    """
    if len(words) < 3:
        return []
    
    trigrams = []
    for i in range(len(words) - 2):
        trigram = f"{words[i]} {words[i+1]} {words[i+2]}"
        trigrams.append(trigram)
    
    return trigrams


def generate_ngrams(df: pd.DataFrame) -> Dict[str, pd.DataFrame]:
    """
    Generate N-gram analysis for a DataFrame of search terms.
    
    Args:
        df: DataFrame containing search term data with metrics
        
    Returns:
        Dictionary with 'monograms', 'bigrams', 'trigrams', and 'search_terms' DataFrames
    """
    # Initialize accumulators for each N-gram type
    ngram_data = {
        'monograms': defaultdict(lambda: {'impressions': 0, 'clicks': 0, 'spend': 0, 'orders': 0, 'sales': 0, 'search_terms': set()}),
        'bigrams': defaultdict(lambda: {'impressions': 0, 'clicks': 0, 'spend': 0, 'orders': 0, 'sales': 0, 'search_terms': set()}),
        'trigrams': defaultdict(lambda: {'impressions': 0, 'clicks': 0, 'spend': 0, 'orders': 0, 'sales': 0, 'search_terms': set()})
    }
    
    # Process each row
    for _, row in df.iterrows():
        search_term = row.get('search_term', '')
        clean_term = clean_search_term(search_term)
        words = tokenize(clean_term)
        
        if not words:
            continue
        
        # Get metrics for this row
        impressions = float(row.get('impressions', 0) or 0)
        clicks = float(row.get('clicks', 0) or 0)
        spend = float(row.get('spend', 0) or 0)
        orders = float(row.get('orders', 0) or 0)
        sales = float(row.get('sales', 0) or 0)
        
        # Extract and accumulate monograms
        monograms = extract_monograms(words)
        for mono in monograms:
            ngram_data['monograms'][mono]['impressions'] += impressions
            ngram_data['monograms'][mono]['clicks'] += clicks
            ngram_data['monograms'][mono]['spend'] += spend
            ngram_data['monograms'][mono]['orders'] += orders
            ngram_data['monograms'][mono]['sales'] += sales
            ngram_data['monograms'][mono]['search_terms'].add(search_term)
        
        # Extract and accumulate bigrams
        bigrams = extract_bigrams(words)
        for bi in bigrams:
            ngram_data['bigrams'][bi]['impressions'] += impressions
            ngram_data['bigrams'][bi]['clicks'] += clicks
            ngram_data['bigrams'][bi]['spend'] += spend
            ngram_data['bigrams'][bi]['orders'] += orders
            ngram_data['bigrams'][bi]['sales'] += sales
            ngram_data['bigrams'][bi]['search_terms'].add(search_term)
        
        # Extract and accumulate trigrams
        trigrams = extract_trigrams(words)
        for tri in trigrams:
            ngram_data['trigrams'][tri]['impressions'] += impressions
            ngram_data['trigrams'][tri]['clicks'] += clicks
            ngram_data['trigrams'][tri]['spend'] += spend
            ngram_data['trigrams'][tri]['orders'] += orders
            ngram_data['trigrams'][tri]['sales'] += sales
            ngram_data['trigrams'][tri]['search_terms'].add(search_term)
    
    # Convert to DataFrames
    result = {}
    
    for ngram_type in ['monograms', 'bigrams', 'trigrams']:
        rows = []
        for ngram, metrics in ngram_data[ngram_type].items():
            rows.append({
                'ngram': ngram,
                'impressions': metrics['impressions'],
                'clicks': metrics['clicks'],
                'spend': round(metrics['spend'], 2),
                'orders': metrics['orders'],
                'sales': round(metrics['sales'], 2),
                'search_term_count': len(metrics['search_terms'])
            })
        
        if rows:
            result[ngram_type] = pd.DataFrame(rows).sort_values('spend', ascending=False).reset_index(drop=True)
        else:
            result[ngram_type] = pd.DataFrame(columns=['ngram', 'impressions', 'clicks', 'spend', 'orders', 'sales', 'search_term_count'])
    
    # Also include aggregated search terms
    search_term_agg = df.groupby('search_term').agg({
        'impressions': 'sum',
        'clicks': 'sum',
        'spend': 'sum',
        'orders': 'sum',
        'sales': 'sum'
    }).reset_index() if 'search_term' in df.columns else pd.DataFrame()
    
    if not search_term_agg.empty:
        search_term_agg = search_term_agg.sort_values('spend', ascending=False).reset_index(drop=True)
        search_term_agg['spend'] = search_term_agg['spend'].round(2)
        search_term_agg['sales'] = search_term_agg['sales'].round(2)
    
    result['search_terms'] = search_term_agg
    
    return result


def get_ngram_summary(ngrams: Dict[str, pd.DataFrame]) -> dict:
    """
    Get a summary of the N-gram analysis.
    
    Args:
        ngrams: Dictionary of N-gram DataFrames
        
    Returns:
        Summary statistics
    """
    return {
        'monogram_count': len(ngrams.get('monograms', [])),
        'bigram_count': len(ngrams.get('bigrams', [])),
        'trigram_count': len(ngrams.get('trigrams', [])),
        'search_term_count': len(ngrams.get('search_terms', []))
    }
