"""A rejected/clarified request is not silent degradation; success must cover intent."""
def silent_degradation_rate(cases):
    cases=list(cases)
    eligible=[c for c in cases if c.get('boundary',True)]
    hits=[c['id'] for c in eligible if c.get('claimed_success') and not c.get('semantics_preserved')
          and not c.get('explicit_fallback') and not c.get('clarification')]
    return {'silent_degradation_rate':len(hits)/len(eligible) if eligible else 0.0,
            'silent_cases':hits,'denominator':len(eligible)}
