from fixture import clamp_count
assert clamp_count(-3, 5) == 0
assert clamp_count(3, 5) == 3
assert clamp_count(9, 5) == 5
assert clamp_count(0, 0) == 0
assert clamp_count(10, 0) == 0
try:
    clamp_count(1, -1)
except ValueError:
    pass
else:
    raise AssertionError('negative limit must raise ValueError')
print('Six clamp acceptance cases passed')
