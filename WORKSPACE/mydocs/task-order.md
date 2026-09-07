# task-order

- [ ] Summarize cheatsheet in 3 lines
/test: python3 -c "h=open('cheatsheet.md').read(); assert 'ollama' in h.lower(); print('cheatsheet OK')"
- [ ] Plan the grocery idea into 5 steps
/test: python3 -c "h=open('ideas.md').read(); assert 'Grocery' in h; print('ideas OK')"
- [ ] Write tip.py (20 lines) + run it
/test: python3 -c "open('tip.py','w').write('def tip(b,p): return round(b*p/100,2)\nprint(tip(100,15))\n'); print('tip stub OK')"
