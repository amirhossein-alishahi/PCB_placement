"""Narrow, evidence-based pin-label consistency check for known parts.

Reference: STMicroelectronics STM32F410x8/B DS11144, LQFP48 pinout and
pin-number table (https://www.st.com/resource/en/datasheet/stm32f410c8.pdf).
Only labels that explicitly name a pin signal are checked. There is NO ERC,
net electrical/type validation, or assertion that naming conventions are right.
"""
from __future__ import annotations

import re

STM32F410_LQFP48 = dict(enumerate((
    'VBAT PC13 PC14 PC15 PH0 PH1 NRST VSSA VDDA PA0 PA1 PA2 PA3 PA4 PA5 PA6 '
    'PA7 PB0 PB1 PB2 PB10 VCAP_1 VSS VDD PB12 PB13 PB14 PB15 PA8 PA9 PA10 '
    'PA11 PA12 PA13 VSS VDD PA14 PA15 PB3 PB4 PB5 PB6 PB7 BOOT0 PB8 VSS PDR_ON VDD'
).split(),start=1))

EXPLICIT_SIGNAL = re.compile(
    r'(?i)(?:^|[^A-Za-z0-9])(?P<ref>U\d+)[_\-]'
    r'(?P<signal>PA\d+|PB\d+|PC\d+|PH\d+|VCAP_?1|NRST|BOOT0|PDR_ON)'
    r'(?=$|[^A-Za-z0-9])')


def check_known_pinout(parts, value_getter):
    results=[]
    for p in parts:
        if value_getter(p).strip().upper()!='STM32F410CBT6' or len(p.pads)!=48:
            continue
        checked=[]; conflicts=[]
        for pad in p.pads:
            found=EXPLICIT_SIGNAL.findall(pad.net or '')
            for ref, signal in found:
                if ref.upper()!=p.name.upper():continue
                number=int(pad.name) if pad.name.isdecimal() else None
                expected=STM32F410_LQFP48.get(number)
                signal=signal.upper().replace('VCAP1','VCAP_1')
                result={'ref':p.name,'pad':pad.name,'signal_label':signal,
                        'expected_function':expected,'net':pad.net}
                if expected==signal:checked.append(result)
                else:conflicts.append(result)
        results.append({'ref':p.name,'part_value':'STM32F410CBT6','package':'LQFP48',
                        'matched_labels':checked,'conflicting_labels':conflicts,
                        'unverified_pin_count':48-len({r['pad'] for r in checked}),
                        'status':'conflicting_pin_labels' if conflicts else 'partial_label_consistency_only',
                        'datasheet':'ST DS11144, LQFP48 pinout; not a manufacturing certification'})
    return results
