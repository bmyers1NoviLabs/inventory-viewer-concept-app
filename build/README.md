Expected data/ layout after `python serve.py --refresh`:

  data/NGL_ForecastWellMonths.tsv         drilled monthly production (wedge)
  data/WellDetails.tsv                    drilled headers (CurrentOperator merge)
  data/WellboreLocations.tsv              drilled sticks (future map layer)
  data/Undrilled_{basin}_*.csv            undrilled production / details / locations
  data/econ/undrilled_{basin}.zip         economics_all + data_plus + pad_economics
  data/econ/drilled_{basin}.zip           PDP economics
  data/static/phase_windows/{basin}.zip   committed
  data/static/pud_res/*.zip               committed
  data/static/pads/{basin}*.zip           committed
