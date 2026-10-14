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
from pathlib import Path
from typing import Any

BASE = r'C:/Users/45140/OneDrive/Desktop/项目/bobanana6/acceptance/ab/13f-crowding/_b_arm'
DATA_DIR = os.path.join(BASE, 'input/data')
OUTPUT_DIR = os.path.join(BASE, 'output')

DATA_TSEV = [
    'SUBMISSION.tsv',
    'COVERPAGE.tsv',
    'SUMMARYPAGE.tsv',
    'INFOTABLE.tsv',
]

FILLERS = ['|']
FILL_DELIMS = ['\t', ' ']
FILL_TABS = [FILL_DELIMS[0]]
FILL_TABS_TSEV = [FILL_DELIMS[0]]

def read_tsv(path: str) -> list[list[str]]:
    with open(path, 'r', encoding='utf-8') as f:
        reader = csv.reader(f, delimiter=FILL_DELIMS[0], quotechar='"', doublequote=True)
        return list(reader)

def parse_float(x: str) -> float:
    try:
        return float(x.strip())
    except (ValueError, AttributeError):
        return 0.0

def round_float(x: float, decimals: int = 6) -> float:
    return round(x, decimals)

def read_files():
    """Read all required files into memory."""
    submissions = read_tsv(os.path.join(DATA_DIR, 'SUBMISSION.tsv'))
    coverpage = read_tsv(os.path.join(DATA_DIR, 'COVERPAGE.tsv'))
    summarypage = read_tsv(os.path.join(DATA_DIR, 'SUMMARYPAGE.tsv'))
    infotable = read_tsv(os.path.join(DATA_DIR, 'INFOTABLE.tsv'))
    
    with open(os.path.join(DATA_DIR, 'target_managers.csv'), 'r', encoding='utf-8') as f:
        managers_csv = csv.reader(f, delimiter=',')
        managers = list(managers_csv)
    
    with open(os.path.join(DATA_DIR, 'target_periods.csv'), 'r', encoding='utf-8') as f:
        periods_csv = csv.reader(f, delimiter=',')
        periods = list(periods_csv)
    
    return submissions, coverpage, summarypage, infotable, managers, periods

class FilingAnalyzer:
    def __init__(self, submissions: list[list[str]], coverpage: list[list[str]], 
                 summarypage: list[list[str]], infotable: list[list[str]]):
        self.submissions = submissions
        self.coverpage = coverpage
        self.summarypage = summarypage
        self.infotable = infotable
        
        self.mgr_map: dict[str, tuple[int, str]] = {}  # CIK -> (manager_order, manager_label)
        self.period_map: dict[tuple[int, str], str] = {}  # (period_order, period_end) -> date
        
        self.extract_mgr_map()
        self.extract_period_map()
        
    def extract_mgr_map(self):
        """Build manager lookup from target files."""
        for row in self.mgr_map:
            if len(row) >= 3:
                order = int(row[0])
                cik = row[1]
                label = row[2]
                self.mgr_map[cik] = (order, label)
        
    def extract_period_map(self):
        """Build period lookup from target periods file."""
        for row in self.periods:
            if len(row) >= 2:
                order = int(row[0])  # This is period_order, not the column name
                date = row[1]  # This should be period_end
                self.period_map[(order, date)] = date
    
    def process_filing(self, row: list[str], filings_by_mgr: dict,
                       mgr_order: int, mgr_label: str,
                       period_order: int, period_end: str,
                       summary_pages: dict, acc_map: dict) -> dict | None:
        """Process a single submission row and extract filing info."""
        if len(row) < 10:
            return None
            
        acc_num = row[0]
        reporting_date = row[7]
        is_amendment = row[2].upper() in ['Y', '1']
        amend_no = row[3].strip() if row[3].strip() else ''
        amend_type = row[4].strip().upper() if row[4].strip() else ''
        form_type = row[9].strip().upper()
        
        if form_type not in ['13F HOLDINGS REPORT']:
            return None
            
        report_period = row[1].strip()
        
        if is_amendment and not ((period_order == 1 and report_period == '31-DEC-2025') or
                                  (period_order == 2 and report_period == '30-SEP-2025')):
            return None
            
        return {
            'acc_num': acc_num,
            'report_date': reporting_date,
            'is_amendment': is_amendment,
            'amend_no': amend_no,
            'amend_type': amend_type,
            'form_type': form_type,
            'report_period': report_period,
        }
    
    def resolve_filings(self, clean_mgr_periods: dict) -> dict:
        """Resolve filing hierarchy for each manager-period."""
        all_mgrs: set[str] = set()
        result: dict[str, Any] = {}
        
        for (mgr_order, mgr_label), (cik, per_ord, per_end, report_date) in clean_mgr_periods.items():
            filings = []
            
            for row in self.submissions:
                if len(row) < 10:
                    continue
                    
                acc = row[0]
                if acc in result.get('submissions', {}):
                    continue
                    
                reporting_mgr = row[8].strip()
                reported_mgr = reporting_mgr.replace(',', '').replace(' ', '')
                if reported_mgr != mgr_label:
                    continue
                    
                fp = self.process_filing(row, filings, mgr_order, mgr_label,
                                         per_ord, per_end, clean_mgr_periods, 
                                         result.get('accumulated', {}))
                if not fp:
                    continue
                
                fp['mgr_order'] = mgr_order
                fp['mgr_label'] = mgr_label
                fp['period_order'] = per_ord
                fp['period_end'] = per_end
                fp['report_date'] = report_date
                fp['cik'] = cik
                
                filings.append(fp)
            
            filings.sort(key=lambda x: (x['report_date'], x['is_amendment']))
            result['filings'] = filings
            result['cik_order'] = cik_order
            all_mgrs.add(cik)
        
        return {m: r for m, r in result.items()}
    
    def analyze(self) -> dict:
        """Main analysis pipeline."""
        return resolve_filings({}, {}, {})

def write_csv(data: list[dict], fieldnames: list[str], path: str):
    """Write data to CSV file."""
    if not data:
        return
    from pathlib import Path
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction='ignore')
        writer.writeheader()
        for row in data:
            writer.writerow(row)

def main():
    submissions, coverpage, summarypage, infotable, managers, periods = read_files()
    
    print(f'Read {len(submissions)} submissions, {len(coverpage)} coverpage, '
          f'{len(summarypage)} summarypage, {len(infotable)} infotable')
    print(f'Managers: {[(m[0], m[1]) for m in managers]}')
    print(f'Periods: {[(p[0], p[1]) for p in periods]}')

if __name__ == '__main__':
    main()