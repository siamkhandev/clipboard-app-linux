from winclip import History, LIMIT

h = History(':memory:')
h.new_boot('boot-1')
h.add('text', 'a')
h.add('text', 'b')
h.add('text', 'a')  # duplicate moves to top, no new row
assert [r[2] for r in h.items()] == ['a', 'b']

pinned_id = h.items()[1][0]
h.pin(pinned_id, True)
for i in range(LIMIT + 5):
    h.add('text', f'x{i}')
texts = [r[2] for r in h.items()]
assert len(texts) == LIMIT + 1 and 'b' in texts  # pinned item survives the trim

h.bump(pinned_id)
assert h.items()[0][2] == 'b'
h.add('image', b'\x89PNG...')
assert h.items()[0][1:3] == ('image', b'\x89PNG...')

h.new_boot('boot-1')  # same boot: nothing cleared
assert len(h.items()) == LIMIT + 1
h.new_boot('boot-2')  # reboot: only pinned left
assert [r[2] for r in h.items()] == ['b']
h.clear()
assert [r[2] for r in h.items()] == ['b']
print('ok')
