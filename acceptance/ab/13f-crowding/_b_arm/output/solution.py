#!/usr/bin/env python3
"""
13F Amendment-Aware Crowding Analysis
Reconstructs effective portfolio state for manager-period pairs
accounting for filings and amendments.
"""

import csv
import json
import os
from collections import defaultdict
from typing import Dict, Any, List, Optional, Tuple

BASE = r'C:/Users/45140/OneDrive/Desktop/项目/bobanana6/acceptance/ab/13f-crowding/_b_arm'
DATA_DIR = os.path.join(BASE, 'input/data')
OUTPUT_DIR = os.path.join(BASE, 'output')

def read_tsv(path: str) -> List[List[str]]:
    with open(path, 'r', encoding='utf-8') as f:
        content = f.read()
        lines = content.split('\t')
        return lines

def read_csv(path: str) -> List[List[str]]:
    with open(path, 'r', encoding='utf-8') as f:
        return list(csv.reader(f, delimiter=','))

def parse_float(x: str) -> float:
    try:
        return float(str(x).strip())
    except (ValueError, TypeError, AttributeError):
        return 0.0

class FilingResolver:
    def __init__(self):
        self.submissions = read_tsv(os.path.join(DATA_DIR, 'SUBMISSION.tsv'))
        self.coverpage = read_tsv(os.path.join(DATA_DIR, 'COVERPAGE.tsv'))
        self.summarypage = read_tsv(os.path.join(DATA_DIR, 'SUMMARYPAGE.tsv'))
        self.infotable = read_tsv(os.path.join(DATA_DIR, 'INFOTABLE.tsv'))
        
        self.mgr_data = read_csv(os.path.join(DATA_DIR, 'target_managers.csv'))
        self.period_data = read_csv(os.path.join(DATA_DIR, 'target_periods.csv'))
        
        self.mgr_lookup = {}
        for row in self.mgr_data[1:]:
            if len(row) >= 3:
                try:
                    self.mgr_lookup[row[1]] = (int(row[0]), row[2])
                except:
                    pass
        
        self.period_lookup = {}
        for row in self.period_data[1:]:
            if len(row) >= 2:
                try:
                    self.period_lookup[(int(row[0]), row[1])] = row[1]
                except:
                    pass
        
        self.infotable_map = defaultdict(list)
        for row in self.infotable[1:]:
            if len(row) >= 2:
                self.infotable_map[row[0].strip()].append(row[1].strip())

    def rebuild_summary_lookup(self):
        self.summary_lookup = defaultdict(dict)
        for row in self.summarypage[1:]:
            if len(row) >= 3:
                cik = row[0].strip()
                period_end = row[1].strip()
                val = parse_float(row[2]) if len(row) > 2 else 0
                shp = parse_float(row[3]) if len(row) > 3 else 0
                self.summary_lookup[(cik, period_end)][row[1:].strip()] = {'value': val, 'shp': shp}

    def process_submissions(self, target_mgr: Tuple, target_period: Tuple):
        results = []
        for row in self.submissions[1:]:
            if len(row) < 12:
                continue
            acc_num = row[0]
            reporting_mgr = row[8].strip()
            reporting_cik = row[4].strip()
            cik = row[5].strip() if len(row) > 5 else ''
            if cik:
                reporting_cik = cik
            reporting_period = row[1].strip()
            is_restat = row[2].strip().upper() in ['Y', '1']
            amend_no = row[3].strip() if len(row) > 3 and row[3].strip() else ''
            amend_type = row[4].strip() if len(row) > 4 and row[4].strip() else ''
            
            if acc_num in [r['acc_num'] for r in results]:
                continue
            
            results.append({
                'acc_num': acc_num,
                'cik': reporting_cik,
                'period_order': target_mgr[0],
                'period_end': target_period[1],
                'is_restat': is_restat,
                'amend_no': amend_no,
                'amend_type': amend_type
            })
        
        # Filter to target manager
        filtered = [r for r in results if r['cik'] in self.mgr_lookup and r['cik'] == target_mgr[1]]
        return filtered

def calculate_manager_metrics(holdings):
    """Calculate concentration and HHI metrics."""
    total_value = sum(h['value'] for h in holdings)
    pos_values = [h['value'] for h in holdings if h['value'] > 0]
    
    if not pos_values:
        return {
            'num_positions': 0,
            'total_value': 0,
            'hhi': 0,
            'effective_num_names': 0
        }
    
    sorted_values = sorted(pos_values, reverse=True)
    max_weight = sorted_values[0] / total_value if total_value > 0 else 0
    hhi = sum((v / total_value) ** 2 for v in sorted_values) if total_value > 0 else 0
    effective_num_names = total_value / max_weight if total_value > 0 and max_weight > 0 else 0
    
    return {
        'num_positions': len(holdings),
        'total_value': round_float(total_value),
        'top_cusip': sorted_values[-1][0] if sorted_values else '',
        'top_issuer_name': '',
        'top_weight': round_float(max_weight),
        'hhi': round_float(hhi),
        'effective_num_names': round_float(effective_num_names)
    }

def calculate_turnover(prev_holdings, curr_holdings):
    """Calculate turnover between two periods."""
    set_prev = set(h['cusip'] for h in prev_holdings)
    set_curr = set(h['cusip'] for h in curr_holdings)
    
    new_names = set_curr - set_prev
    exited_names = set_prev - set_curr
    common_names = set_prev & set_curr
    
    total_value_prev = sum(h['value'] for h in prev_holdings)
    total_value_curr = sum(h['value'] for h in curr_holdings)
    
    value_turnover = 0
    if total_value_prev and total_value_curr:
        value_turnover = (total_value_prev + total_value_curr) / 2
    
    return {
        'turnover': value_turnover,
        'new_names_count': len(new_names),
        'exited_names_count': len(exited_names),
        'common_names_count': len(common_names)
    }

def calculate_overlap(holdings_i, holdings_j, period_end):
    """Calculate pairwise overlap between two managers."""
    set_i = set(h['cusip'] for h in holdings_i)
    set_j = set(h['cusip'] for h in holdings_j)
    
    common_cusips = set_i & set_j
    common_names = len(common_cusips)
    
    total_value_i = sum(h['value'] for h in holdings_i)
    total_value_j = sum(h['value'] for h in holdings_j)
    
    weighted_overlap = 0
    for cusip in common_cusips:
        val_i = next((h['value'] for h in holdings_i if h['cusip'] == cusip), 0)
        val_j = next((h['value'] for h in holdings_j if h['cusip'] == cusip), 0)
        weighted_overlap += val_i + val_j
    
    return {
        'weighted_overlap': round_float(weighted_overlap),
        'common_names_count': common_names
    }

def main():
    print("Loading data...")
    filing_resolver = FilingResolver()
    rebuilding_summary_lookup = FilingResolver()
    rebuild
    filing_resolver.rebuild_summary_lookup()
    rebuilding_summary_lookup.rebuild_summary_lookup()
    
    target_mgrs = filing_resolver.mgr_lookup.keys()
    target_periods = filing_resolver.period_lookup.keys()
    
    print(f"Target managers: {len(target_mgrs)}")
    print(f"Target periods: {len(target_periods)}")
    
    all_managed_holds = defaultdict(list)
    all_mgr_period_data = []
    filing_rows = []
    cleaning_audit_rows = []
    
    for cik in target_mgrs:
        target_mgr_order, target_mgr_label = filing_resolver.mgr_lookup[cik]
        print(f"\nProcessing manager {cik}...")
        
        # Get available periods for this manager
        available_periods = filing_resolver.period_lookup.keys()
        for target_period in available_periods:
            target_period_order, target_period_date = target_period
            print(f"  Getting periods for {cik}...")
            
            filings = filing_resolver.process_submissions((cik, target_period_order), (target_period_order, target_period_date))
            
            # Filter filings
            if not filings:
                continue
            
            # Process each filing
            all_mgr_holds = defaultdict(list)
            cleaning_stats = {'raw_effective_rows': 0, 'kept_rows': 0, 'filtered_blank_cusip_rows': 0,
                            'filtered_nonsh_rows': 0, 'filtered_putcall_rows': 0, 'filtered_nonpositive_rows': 0}
            
            for filing in filings:
                acc_num = filing['acc_num']
                rows = filing_resolver.get_submission_rows(acc_num)
                
                if not rows:
                    continue
                
                # Process submission rows
                for row in rows:
                    cleaning_stats['raw_effective_rows'] += 1
                    
                    cusip = row['cusip']
                    if not cusip or cusip == '':
                        cleaning_stats['filtered_blank_cusip_rows'] += 1
                        continue
                    
                    shp = row.get('shp', row.get('sshpr', 'S')) != 'S' or shp != 'S'
                    if shp != 'S':
                        print(f"Found row with shp != S")
                        # Note: The logic needs to be refined
                        continue
                    
                    putcall = row.get('is_put_call', row[10].strip().upper() in ['PY', 'CP'])
                    value = row.get('value', 0)
                    
                    # Clean rows
                    if shp and not putcall and value > 0:
                        cleaning_stats['kept_rows'] += 1
                        # Record position
                        all_mgr_holds[(cik, target_period_order)].append({
                            'cusip': cusip,
                            'value': value,
                            'shp': shp,
                            'row_data': row
                        })
                    else:
                        if not shp:
                            cleaning_stats['filtered_nonsh_rows'] += 1
                        elif putcall:
                            cleaning_stats['filtered_putcall_rows'] += 1
                        elif value <= 0:
                            cleaning_stats['filtered_nonpositive_rows'] += 1
                    
                    if cleaning_stats['filtered_nonsh_rows'] > 0:
                        print(f"Found non-sh row")
                    elif cleaning_stats['filtered_putcall_rows'] > 0:
                        print(f"Found put/call row")
                    elif cleaning_stats['filtered_nonpositive_rows'] > 0:
                        print(f"Found non-positive row")
                    else:
                        print(f"Found clean row")
                    cleaning_stats['raw_effective_rows'] += 1
                    
                    if cleaning_stats['kept_rows'] > 0:
                        print(f"Found kept row")
                
                # Write cleaning stats
                cleaning_audit_rows.append({
                    'cik': filing['cik'],
                    'period_order': target_period_order,
                    'period_end': target_period_date,
                    'raw_effective_rows': cleaning_stats['raw_effective_rows'],
                    'kept_rows': cleaning_stats['kept_rows'],
                    'filtered_blank_cusip_rows': cleaning_stats['filtered_blank_cusip_rows'],
                    'filtered_nonsh_rows': cleaning_stats['filtered_nonsh_rows'],
                    'filtered_putcall_rows': cleaning_stats['filtered_putcall_rows'],
                    'filtered_nonpositive_rows': cleaning_stats['filtered_nonpositive_rows']
                })
    
    print(f"\nAnalysis complete!")
    print(f"Total manager-period pairs: {len(all_mgr_period_data)}")

if __name__ == '__main__':
    main()