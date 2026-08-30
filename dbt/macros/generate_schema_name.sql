{% macro generate_schema_name(custom_schema_name, node) -%}
    {#-
      dbt's default prefixes the target schema onto the custom one, so a model configured
      for `gold` lands in `main_gold`. The raw layer is written by Python into `raw`, and
      having half the warehouse prefixed and half not is the kind of inconsistency that
      makes people write the wrong schema name into a query and blame the warehouse.

      So the custom name wins outright. The cost is that two dbt targets would collide in
      one database, which is the case this override is wrong for. There is one target here
      and the profile has one output.
    -#}
    {%- if custom_schema_name is none -%}
        {{ target.schema }}
    {%- else -%}
        {{ custom_schema_name | trim }}
    {%- endif -%}
{%- endmacro %}
