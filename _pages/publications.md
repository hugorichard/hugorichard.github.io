---
layout: archive
title: "Publications"
permalink: /publications/
author_profile: true
---
{% include base_path %}

{% assign publication_groups = "rl-online-learning:RL and Online Learning|differential-privacy:Differential Privacy|neuroimaging:Neuroimaging" | split: "|" %}

{% for group in publication_groups %}
  {% assign group_parts = group | split: ":" %}
  {% assign group_key = group_parts[0] %}
  {% assign group_title = group_parts[1] %}

  <h2>{{ group_title }}</h2>

  {% for post in site.publications reversed %}
    {% if post.research_areas contains group_key %}
      {% include archive-single-publication.html %}
    {% endif %}
  {% endfor %}
{% endfor %}
